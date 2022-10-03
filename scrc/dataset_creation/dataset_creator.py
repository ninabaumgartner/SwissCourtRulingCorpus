import abc
import copy
import math
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Union
import ast
import seaborn as sns
import plotly.express as px
import matplotlib.pyplot as plt
import datasets
from datasets import DatasetDict, concatenate_datasets

from scrc.enums.cantons import Canton
from scrc.data_classes.ruling_citation import RulingCitation

import dask.dataframe as dd
import numpy as np
import pandas as pd
from scrc.enums.section import Section

from scrc.preprocessors.abstract_preprocessor import AbstractPreprocessor
from scrc.utils.log_utils import get_logger
import json
from scrc.utils.main_utils import retrieve_from_cache_if_exists, save_df_to_cache, get_canton_from_chamber, \
    get_court_from_chamber

from scrc.utils.sql_select_utils import get_legal_area, join_tables_on_decision, legal_areas, get_region, \
    where_string_spider, where_string_court

# pd.options.mode.chained_assignment = None  # default='warn'
sns.set(rc={"figure.dpi": 300, 'savefig.dpi': 300})
sns.set_style("whitegrid")
"""
Extend datasets with big cantonal courts? => only if it does not take too much time (1-2 days per court)
Datasets to be created:
- Judgments
    - Judgment prediction BGer:
        - text classification
        - input (considerations/facts) to label (judgment)
        - Why? 
- Citations
    - Citation prediction
        - multilabel text classification
        - input (entire text with citations removed) to label (cited laws and court decisions)
            labels:
                - all possible rulings
                - most frequent rulings
                - all law articles
                - most frequent law articles
                - law books (without article number)
        - Features:
            - Zero/One/Few Shot
        - Why?: 
    - Citation labeling (similar: https://github.com/reglab/casehold):
        - fill mask
        - input (entire text with citations removed by mask token)
        - Tasks: 
            - predict citation type (law or ruling) or citation
            - multiple choice question answering (choose from say 5 options)
        - Why?: difficult LM Pretraining task
    - Semantic Textual Similarity
        - regression (similarity of two decisions)
        - input: two decision texts
        - label: similarity (between 0 and 1)
        - the more citations are in common, the more similar two decisions are
        - the more similar the common citations the higher the similarity of the decisions
- Criticality Prediction
    - level of appeal prediction
        - text classification/regression (error of model makes more sense))
        - input: facts and considerations (remove information which makes it obvious)
        - label: predict level of appeal of current case
        - Why?: 
    - controversial case prediction
        - (binary) text classification
        - input: entire case of a local court (1st level of appeal)
        - label: predict top level of appeal the case will end up in
        - Why?: very interesting conclusions contentwise 
            (e.g. if we predict that the case might end up in the supreme court => speed up process because the case is very important)
    - case importance prediction (criticality)
        - regression task
        - input: entire text
        - label: level of criticality (normalized citation count (e.g. 1 to 10, 0 to 1))
        - Why?: Can the criticality be determined only by the text? Are specific topics very controversial?
- Chamber/court prediction (proxy for legal area)
    - text classification
    - input (facts) to label (chamber/court code)
    - Features:
        - Zero/One/Few Shot
    - Why?: proxy for legal area prediction to help lawyers find suitable laws
- LM Pretraining:
    - masked language modeling
    - input (entire text)
    - Features:
        - largest openly published corpus of court decisions
    - Why?: train Swiss Legal BERT


- To maybe be considered later 
    - Section splitting: 
        - comment: if it can be split with regexes => not interesting
            - if the splitting is non-standard => more interesting, but manual annotation needed!
            - similar to paper: Structural Text Segmentation of Legal Documents
            - structure can be taken from html (=> labels) and input can be raw text => easy way to get a lot of ground truth!
                - for example splitting into coherent paragraphs
        - token classification (text zoning task), text segmentation
        - input (entire text) to label (section tags per token)
        - Why?: 
    - Date prediction
        - text classification
        - input (entire text) to label (date in different granularities: year, quarter, regression)
        - Features:
            - Zero/One/Few Shot
        - Why?: learn temporal data shift
        - not sure if interesting

Features:
- Time-stratified: train, val and test from different time ranges to simulate more realistic test scenario
- multilingual: 3 languages
- diachronic: more than 20 years
- very diverse: 3 languages, 26 cantons, 112 courts, 287 chambers, most if not all legal areas and all Swiss levels of appeal
"""

"""
Further projects: (inspired by https://arxiv.org/pdf/2106.10776.pdf)
Investigate Legal Citation Prediction as a Natural Language Generation Problem
Citation Prediction only on context of the citation not on the entire document (simulating the writing of a decision by a clerk)
"""


class DatasetCreator(AbstractPreprocessor):
    """
    TODO look at this project for easy data reports: https://pandas-profiling.github.io/pandas-profiling/docs/master/rtd/pages/introduction.html
    TODO alternative for project above: https://dataprep.ai/
    Retrieves the data and preprocesses it for subdatasets of SCRC.
    Also creates the necessary files for a kaggle dataset and a huggingface dataset.
    """

    def __init__(self, config: dict):
        __metaclass__ = abc.ABCMeta
        super().__init__(config)
        self.logger = get_logger(__name__)

        self.seed = 42
        self.minFeatureColLength = 100  # characters
        self.debug_chunksize = 100
        self.real_chunksize = 1_000_000
        self.counter = 0
        self.start_years = {"train": 2002, "validation": 2016, "test": 2018, "secret_test": 2020}
        self.current_year = date.today().year
        self.metadata = ['year', 'legal_area', 'chamber', 'court', 'canton', 'region',
                         'origin_chamber', 'origin_court', 'origin_canton', 'origin_region']

        def build_info_df(table_name, col_name):
            info_df = next(self.select(self.get_engine(self.db_scrc), table_name))
            info_dict = {}
            for index, row in info_df.iterrows():
                info_dict[int(row[f'{table_name}_id'])] = str(row[col_name])
            return info_dict

        self.chamber_dict = build_info_df('chamber', 'chamber_string')
        # self.court_dict = build_info_df('court', 'court_string')
        # self.canton_dict = build_info_df('canton', 'short_code')

        self.debug = True  # to be overridden
        self.overwrite_cache = True  # to be overridden
        self.split_type = None  # to be overridden
        self.dataset_name = None  # to be overridden
        self.feature_cols = [Section.FULL_TEXT]  # to be overridden
        self.labels = []  # to be overridden
        self.available_bges = []  # to be overridden

    @abc.abstractmethod
    def prepare_dataset(self, save_reports):
        pass

    def get_chunksize(self):
        if self.debug:
            return int(self.debug_chunksize)  # run on smaller dataset for testing
        else:
            return int(self.real_chunksize)

    def load_rulings(self):
        """
        Load all bge cases and store in available_bges
        """
        where_string = f"d.decision_id IN {where_string_spider('decision_id', 'CH_BGE')}"
        table_string = 'decision d LEFT JOIN file_number ON file_number.decision_id = d.decision_id'
        decision_df = next(
            self.select(self.get_engine(self.db_scrc), table_string, 'd.*, file_number.text', where_string,
                        chunksize=self.get_chunksize()))
        self.logger.info(f"BGE: There are {len(decision_df.index)} in db (also old or not referenced included).")
        return set(decision_df.text.tolist())

    def get_citation(self, citations_as_string, type):
        """
        extract for each bger all ruling citations
        :param citations_as_string:         citations how they were found in text of bger
        :param cit_type:
        :return:                            dataframe with additional column 'ruling_citation'
        """
        self.counter = self.counter + 1
        if int(self.counter) % 10000 == 0:
            self.logger.info("Processed another 10'000 citations")
        cits = []
        try:
            citations = ast.literal_eval(citations_as_string)  # parse dict string to dict again
            for citation in citations:
                try:
                    cit = citation['text']
                    citation_type = citation['name']
                    cit = ' '.join(cit.split())  # remove multiple whitespaces inside
                    if citation_type == "ruling" and type == 'ruling':
                        cited_file = self.get_file_number(cit)
                        cits.append(cited_file)
                    elif citation_type == "law" and type == 'law':
                        tmp = self.get_law_citation(cit)
                        if tmp is not None:
                            cits.append(tmp)
                except ValueError as ve:
                    self.logger.info(f"Citation has invalid syntax: {citation}")
                    continue
        except ValueError as ve:
            self.logger.info(f"Citations could not be extracted to dict: {citations_as_string}")
        if cits:  # only return something if we actually have citations
            return cits

    def get_file_number(self, citation):
        """
        find for each citation string the matching citation from the start of bge (first page)
        :param citation:         citation as string as found in text
        :return:                 RulingCitation always in German
        """
        # TODO scrape for all bge file number
        # handle citation always in German
        found_citation = RulingCitation(citation, 'de')
        if str(found_citation) in self.available_bges:
            return found_citation.cit_string()
        else:
            # find closest bge with smaller page_number
            year = found_citation.year
            volume = found_citation.volume
            page_number = found_citation.page_number
            new_page_number = -1
            for match in self.available_bges:
                if f"BGE {year} {volume}" in match:
                    tmp = RulingCitation(match, 'de')
                    if new_page_number < tmp.page_number <= page_number:
                        new_page_number = tmp.page_number
            # make sure new page number is not unrealistic far away.
            if page_number - new_page_number < 20:
                result = RulingCitation(f"{year} {volume} {new_page_number}", 'de')
                return result.cit_string()
            return found_citation.cit_string()

    def get_law_citation(self, citations_text):
        """
        handle single law citation
        """
        raise NotImplementedError("This method should be implemented in the subclass.")

    def get_dataset_folder(self):
        if self.debug:
            # make sure that we don't overwrite progress in the real directory
            return self.create_dir(self.tmp_subdir, self.dataset_name)
        return self.create_dir(self.datasets_subdir, self.dataset_name)

    def create_dataset(self, sub_datasets=False, kaggle=False, save_reports=False):
        """
        Retrieves the respective function named by the dataset and executes it to get the df for that dataset.
        :return:
        """
        self.logger.info(f"Creating {self.dataset_name} dataset")

        # TODO in the future: maybe save text as list of paragraphs
        # TODO make sure that the same data is saved to kaggle, csv and huggingface format!

        dataset, labels = self.prepare_dataset(save_reports)
        dataset = dataset.shuffle(seed=42)
        self.save_dataset(dataset, labels, self.get_dataset_folder(), self.split_type,
                          sub_datasets=sub_datasets, kaggle=kaggle, save_reports=save_reports)

    def save_huggingface_dataset(self, splits, feature_col_folder):
        """
        save data as huggingface dataset with columns:
        'id', 'date', 'year', 'language',
        'origin_region', 'origin_canton', 'origin_court', 'origin_chamber', 'legal_area',
        'bge_label', 'citation_label', all feature cols
        :param splits:                  specifying splits of dataset
        :param feature_col_folder:      name of folder
        """
        huggingface_dir = self.create_dir(feature_col_folder, 'huggingface')
        self.logger.info(f"Generating huggingface dataset at {huggingface_dir}")

        for split, dataset in splits.items():
            cols_to_include = ['decision_id', 'language'] + self.metadata + self.labels + self.get_feature_col_names()
            cols_to_remove = [col for col in dataset.column_names if col not in cols_to_include]
            dataset = dataset.remove_columns(cols_to_remove)
            hf_file = f'{huggingface_dir}/{split}.jsonl'

            self.logger.info(f"Saving {split} dataset at {hf_file}")
            dataset.to_json(hf_file, orient='records', lines=True, force_ascii=False)

            self.logger.info(f"Compressing {split} dataset at {hf_file}")
            os.system(f'xz -zkf -T0 {hf_file}')  # -TO to use multithreading

    def get_df(self, engine, data_to_load: dict, court_string="CH_BGer", use_cache=True, overwrite_cache=False):
        """
        get dataframe of all cases and add additional information such as judgments, sections, file_number, citations
        :param engine:          engine used for db connection
        :param data_to_load:    a dict of booleans specifying which data to load
        :param court_string:    defines which court to load data from
        :param overwrite_cache: whether to load the data from the cache if it exists or whether to load it anew from the db
        :return:                dataframe with all data
        """
        if use_cache:
            cache_file = self.data_dir / '.cache' / f'{self.dataset_name}_{self.get_chunksize()}.parquet.gzip'
            # if cached just load it from there
            if not overwrite_cache:
                df = retrieve_from_cache_if_exists(cache_file)
                if not df.empty:
                    return df

        # otherwise query it from the database
        self.logger.info(f"Retrieving the data from the database for court {court_string}")

        df = self.load_decision(court_string, engine)
        if df.empty:
            self.logger.info(f"Did not find any decisions. Skipping court {court_string}")
            return df  # return right away so we don't run into errors

        df.rename(columns={'lang': 'language'}, inplace=True)
        decision_ids = ["'" + str(x) + "'" for x in df['decision_id'].tolist()]

        if data_to_load['section']:
            df = self.load_section(decision_ids, df, engine, court_string)
        if data_to_load['file']:
            df = self.load_file(df, engine)
        if data_to_load['file_number']:
            df = self.load_file_number(decision_ids, df, engine)
        if data_to_load['judgment']:
            df = self.load_judgment(decision_ids, df, engine)
        if data_to_load['citation']:
            df = self.load_citation(decision_ids, df, engine)
        if data_to_load['lower_court']:
            df = self.load_lower_court(decision_ids, df, engine, court_string)

        self.logger.info("Finished loading the data from the database")
        if use_cache:
            save_df_to_cache(df, cache_file)
        return df

    def load_decision(self, court_string, engine):
        self.logger.info("Loading Decision")
        table = 'decision d LEFT JOIN language ON language.language_id = d.language_id'
        columns = 'd.*, extract(year from d.date) as year, language.iso_code as lang'
        where = f"d.decision_id IN {where_string_court('decision_id', court_string)}"
        return next(self.select(engine, table, columns, where, chunksize=self.get_chunksize()))

    def load_file_number(self, decision_ids, df, engine):
        self.logger.info('Loading File Number')
        table = f"{join_tables_on_decision(['file_number'])}"
        where = f"file_number.decision_id IN ({','.join(decision_ids)})"
        file_number_df = next(self.select(engine, table, "file_numbers", where, None, self.get_chunksize()))

        # we get a list of file_numbers but only want one, all entries are the same but different syntax
        def get_one_file_number(column_data):
            file_number = str(next(iter(column_data or []), None))
            file_number = file_number.replace(" ", "_")
            file_number = file_number.replace(".", "_")
            return file_number

        df['file_number'] = file_number_df['file_numbers'].map(get_one_file_number)
        return df

    def load_section(self, decision_ids, df, engine, court_string):
        # TODO this could probably be sped up if we just load the sections we need
        self.logger.info('Loading Section')
        table = f"{join_tables_on_decision(['num_tokens'])}"
        where = f"section.decision_id IN ({','.join(decision_ids)})"
        section_df = next(self.select(engine, table, "sections", where, None, self.get_chunksize()))
        df['sections'] = section_df['sections']

        for feature_col in self.get_feature_col_names():
            df = self.expand_df(df, feature_col)

        df['chamber'] = df.chamber_id.apply(self.get_string_value, args=[self.chamber_dict])  # chamber
        df['court'] = df.chamber.apply(get_court_from_chamber)  # court: first two parts of chamber_string
        df['canton'] = df.chamber.apply(get_canton_from_chamber)  # canton: first part of chamber_string
        df['region'] = df.canton.apply(get_region)

        if court_string == "CH_BGer":
            df['legal_area'] = df.chamber_id.apply(get_legal_area)
        else:
            df['legal_area'] = np.nan
        return df

    def get_feature_col_names(self):
        return [feature_col.name.lower() for feature_col in self.feature_cols]

    def load_citation(self, decision_ids, df, engine):
        self.logger.info('Loading Citation')
        table = f"{join_tables_on_decision(['citation'])}"
        where = f"citation.decision_id IN ({','.join(decision_ids)})"
        citations_df = next(self.select(engine, table, "citations", where, None, self.get_chunksize()))
        df['citations'] = citations_df['citations'].astype(str)
        return df

    def load_file(self, df, engine):
        self.logger.info('Loading File')
        table = f"{join_tables_on_decision(['file'])}"
        columns = 'file.file_name, file.html_url, file.pdf_url'
        file_ids = ["'" + str(x) + "'" for x in df['file_id'].tolist()]
        where = f"file.file_id IN ({','.join(file_ids)})"
        file_df = next(self.select(engine, table, columns, where, None, self.get_chunksize()))
        df['file_name'] = file_df['file_name']
        df['html_url'] = file_df['html_url']
        df['pdf_url'] = file_df['pdf_url']
        return df

    def load_judgment(self, decision_ids, df, engine):
        self.logger.info('Loading Judgments')
        table = f"{join_tables_on_decision(['judgment'])}"
        where = f"judgment_map.decision_id IN ({','.join(decision_ids)})"
        judgments_df = next(self.select(engine, table, "judgments", where, None, self.get_chunksize()))
        df['judgments'] = judgments_df['judgments'].astype(str)
        return df

    def load_lower_court(self, decision_ids, df, engine, court_string):
        self.logger.info('Loading Lower Court')
        table = f"{join_tables_on_decision(['lower_court'])}"
        columns = ("lower_court.date as origin_date,"
                   "lower_court.court_id as origin_court, "
                   "lower_court.canton_id as origin_canton, "
                   "lower_court.chamber_id as origin_chamber, "
                   "lower_court.file_number as origin_file_number")
        where = f"lower_court.decision_id IN ({','.join(decision_ids)})"
        lower_court_df = next(self.select(engine, table, columns, where, None, self.get_chunksize()))
        df['origin_file_number'] = lower_court_df['origin_file_number']
        df['origin_date'] = lower_court_df['origin_date']
        df['origin_chamber'] = lower_court_df['origin_chamber']
        df['origin_court'] = lower_court_df['origin_court']
        df['origin_canton'] = lower_court_df['origin_canton']

        if court_string == 'CH_BGer':
            df['origin_chamber'] = df.origin_chamber.apply(self.get_string_value, args=[self.chamber_dict])
            df['origin_court'] = df.origin_chamber.apply(get_court_from_chamber)
            df['origin_canton'] = df.origin_chamber.apply(get_canton_from_chamber)
            df['origin_region'] = df.origin_canton.apply(get_region)
        else:
            df['origin_chamber'] = np.nan
            df['origin_court'] = np.nan
            df['origin_canton'] = np.nan
            df['origin_region'] = np.nan
        return df

    @staticmethod
    def get_string_value(x, info_dict):
        if not math.isnan(float(x)):
            return info_dict[int(x)]
        else:
            return np.nan

    def expand_df(self, df, feature_col):
        """
        remove not usable values from dataframe, add num_tokens for each feature_col
        :param df:      dataframe containing all the data
        :param feature_col:  specifying column (=feature_col) which is cleaned
        :return:        dataframe
        """

        # replace empty and whitespace strings with nan so that they can be removed
        def filter_column(row, section_attr):
            if not isinstance(row, str) and not isinstance(row, list): return np.nan
            if isinstance(row, str):
                row = ast.literal_eval(row)  # convert string to list of dicts
            for section in row:
                if section['name'] == feature_col:
                    return section[section_attr]

        df[feature_col] = df['sections'].apply(filter_column, section_attr='section_text')

        # replace empty strings with nan so that they can be removed
        df[feature_col] = df[feature_col].replace(r'^\s+$', np.nan, regex=True)
        df[feature_col] = df[feature_col].replace('', np.nan)

        df[f"{feature_col}_num_tokens_bert"] = df['sections'].apply(filter_column, section_attr='num_tokens_bert')
        df[f"{feature_col}_num_tokens_spacy"] = df['sections'].apply(filter_column, section_attr='num_tokens_spacy')
        df[f"{feature_col}_num_tokens_bert"] = df[f"{feature_col}_num_tokens_bert"].fillna(value=0).astype(int)
        df[f"{feature_col}_num_tokens_spacy"] = df[f"{feature_col}_num_tokens_spacy"].fillna(value=0).astype(int)

        if self.split_type == "date-stratified":
            df = df.dropna(subset=['year'])  # make sure that each entry has an associated year
            df.year = df.year.astype(int)  # convert from float to nicer int
        df.decision_id = df.decision_id.astype(str)  # convert from uuid to str so it can be saved

        return df

    def save_dataset(self, dataset: datasets.Dataset, labels: list, folder: Path,
                     split_type="date-stratified", sub_datasets=False, kaggle=False, save_reports=False):
        """
        creates all the files necessary for a kaggle dataset from a given df
        :param dataset:     the huggingface dataset to save
        :param labels:      list of all the labels
        :param folder:      where to save the files
        :param split_type:  "date-stratified", "random", or "all_train"
        :param sub_datasets:whether or not to create the special sub dataset for testing of biases
        :param kaggle:      whether or not to create the special kaggle dataset
        :param save_reports:whether or not to compute and save reports
        :return:
        """
        splits = self.create_splits(dataset, split_type, include_all=save_reports)
        self.save_splits(splits, labels, folder, save_reports=save_reports)
        self.save_huggingface_dataset(splits, self.get_dataset_folder())

        if sub_datasets:
            sub_datasets_dict = self.create_sub_datasets(splits, split_type)
            sub_datasets_dir = self.create_dir(folder, 'sub_datasets')
            for category, sub_dataset_category in sub_datasets_dict.items():
                self.logger.info(f"Processing sub dataset category {category}")
                category_dir = self.create_dir(sub_datasets_dir, category)
                for sub_dataset, sub_dataset_splits in sub_dataset_category.items():
                    sub_dataset_dir = self.create_dir(category_dir, sub_dataset)
                    self.save_splits(sub_dataset_splits, labels, sub_dataset_dir, save_csvs=['test'])

        if kaggle:
            # save special kaggle files
            kaggle_splits = self.prepare_kaggle_splits(splits)
            kaggle_dir = self.create_dir(folder, 'kaggle')
            self.save_splits(kaggle_splits, labels, kaggle_dir, save_reports=save_reports)

        self.logger.info(f"Saved dataset files to {folder}")
        return splits

    def prepare_kaggle_splits(self, splits):
        self.logger.info("Saving the data in kaggle format")
        # deepcopy splits, so we don't mess with the original dict
        kaggle_splits = copy.deepcopy(splits)
        # create solution file
        kaggle_splits['solution'] = kaggle_splits['test'].drop('text', axis='columns')  # drop text
        # rename according to kaggle conventions
        kaggle_splits['solution'] = kaggle_splits['solution'].rename(columns={"label": "Expected"})
        # create test file
        kaggle_splits['test'] = kaggle_splits['test'].drop('label', axis='columns')  # drop label
        # create sampleSubmission file
        # rename according to kaggle conventions
        sample_submission = kaggle_splits['solution'].rename(columns={"Expected": "Predicted"})
        # set to random value
        sample_submission['Predicted'] = np.random.choice(kaggle_splits['solution']['Expected'],
                                                          size=len(kaggle_splits['solution']))
        kaggle_splits['sample_submission'] = sample_submission
        return kaggle_splits

    def save_splits(self, splits: dict, labels: list, folder: Path,
                    save_reports=True, save_csvs: Union[list, bool] = True):
        """
        Saves the splits to the filesystem and generates reports
        :param splits:          the splits dictionary to be saved
        :param labels:          list of labels to be saved
        :param folder:          where to save the splits
        :param save_reports:    whether to save reports
        :param save_csvs:       whether to save csv files
        :return:
        """
        self.save_labels(labels, folder)
        for split, dataset in splits.items():
            if len(dataset) < 2:
                self.logger.info(f"Skipping split {split} because "
                                 f"{len(dataset)} entries are not enough to create reports.")
                continue
            self.logger.info(f"Processing split {split}")

            if save_reports or save_csvs:
                # without the feature_cols, the dataset should fit into RAM
                # Additionally, we don't want to save the long text columns to the csv files because it becomes unreadable
                self.logger.info(f"Exporting metadata columns of dataset to pandas dataframe for eaiser plotting")
                df = dataset.remove_columns(self.get_feature_col_names()).to_pandas()
            if save_reports:
                self.logger.info(f"Computing metadata reports")
                self.save_report(folder, split, df)

            if save_csvs:
                if isinstance(save_csvs, list):
                    if split not in save_csvs:
                        continue  # Only save if the split is in the list
                self.logger.info("Saving csv file")
                df.to_csv(folder / f"{split}.csv", index_label='id', index=False)

    def create_splits(self, dataset, split_type, include_all=False):
        self.logger.info(f"Dividing data into splits based on split_type: {split_type}")
        if split_type == "random":
            train, val, test = self.split_random(dataset)
            splits = {'train': train, 'val': val, 'test': test}
        elif split_type == "date-stratified":
            train, val, test, secret_test = self.split_date_stratified(dataset, self.start_years)
            splits = {'train': train, 'val': val, 'test': test, 'secret_test': secret_test}
        elif split_type == "all_train":
            splits = {'train': dataset}  # no split at all
        else:
            raise ValueError("Please supply a valid split_type")
        if include_all:
            # we need to update it since some entries have been removed
            splits['all'] = concatenate_datasets(list(splits.values()))

        return splits

    def create_sub_datasets(self, splits, split_type):
        """
        Creates sub datasets for applications extending beyond the normal splits
        :param split_type:  the type of splitting the data (date-stratified or random)
        :param splits:      the dictionary containing the split dataframes
        :return:
        """
        self.logger.info("Creating sub datasets")
        # TODO debug this

        # set up data structure
        sub_datasets_dict = {metadata: dict() for metadata in self.metadata}
        sub_datasets_dict['input_length'] = dict()

        self.logger.info(f"Processing sub dataset input_length")
        boundaries = [0, 512, 1024, 2048, 4096, 8192]
        for i in range(len(boundaries) - 1):
            lower, higher = boundaries[i] + 1, boundaries[i + 1]
            sub_dataset = sub_datasets_dict['input_length'][f'between({lower:04d},{higher:04d})'] = dict()
            for split_name, split_df in splits.items():
                sub_dataset[split_name] = split_df[split_df.num_tokens_bert.between(lower, higher)]

        self.logger.info(f"Processing sub dataset year")
        if split_type == "date-stratified":
            for year in range(self.start_years["test"], self.current_year):
                sub_dataset = sub_datasets_dict['year'][str(year)] = dict()
                for split_name, split_df in splits.items():
                    sub_dataset[split_name] = split_df[split_df.year == year]

        self.logger.info(f"Processing sub dataset legal_area")
        for legal_area in legal_areas.keys():
            sub_dataset = sub_datasets_dict['legal_area'][legal_area] = dict()
            for split_name, split_df in splits.items():
                sub_dataset[split_name] = split_df[split_df.legal_area.str.contains(legal_area)]

        self.logger.info(f"Processing sub dataset origin_region")
        for region in splits['all'].origin_region.dropna().unique().tolist():
            sub_dataset = sub_datasets_dict['origin_region'][region] = dict()
            for split_name, split_df in splits.items():
                region_df = split_df.dropna(subset=['origin_region'])
                sub_dataset[split_name] = region_df[region_df.origin_region.str.contains(region)]

        self.logger.info(f"Processing sub dataset origin_canton")
        for canton in splits['all'].origin_canton.dropna().unique().tolist():
            sub_dataset = sub_datasets_dict['origin_canton'][canton] = dict()
            for split_name, split_df in splits.items():
                canton_df = split_df.dropna(subset=['origin_canton'])
                sub_dataset[split_name] = canton_df[canton_df.origin_canton.str.contains(canton)]

        self.logger.info(f"Processing sub dataset origin_court")
        for court in splits['all'].origin_court.dropna().unique().tolist():
            sub_dataset = sub_datasets_dict['origin_court'][court] = dict()
            for split_name, split_df in splits.items():
                court_df = split_df.dropna(subset=['origin_court'])
                sub_dataset[split_name] = court_df[court_df.origin_court.str.contains(court)]

        self.logger.info(f"Processing sub dataset origin_chamber")
        for chamber in splits['all'].origin_chamber.dropna().unique().tolist():
            sub_dataset = sub_datasets_dict['origin_chamber'][chamber] = dict()
            for split_name, split_df in splits.items():
                chamber_df = split_df.dropna(subset=['origin_chamber'])
                sub_dataset[split_name] = chamber_df[chamber_df.origin_chamber.str.contains(chamber)]

        return sub_datasets_dict

    def save_report(self, folder, split, df):
        """
        Saves statistics about the dataset in the form of csv tables and png graphs.
        :param folder:  the base folder to save the report to
        :param split:   the name of the split
        :param df:      the df containing the dataset
        :return:
        """
        self.logger.info(f"Saving report for split {split}")
        split_folder = self.create_dir(folder, f'reports/{split}')
        for attribute in self.metadata:
            self.plot_barplot_attribute(df, split_folder, attribute)

        for feature_col in self.get_feature_col_names():
            dict = {f'{feature_col}_num_tokens_bert': 'num_tokens_bert',
                    f'{feature_col}_num_tokens_spacy': 'num_tokens_spacy'}
            self.plot_input_length(df.rename(columns=dict), split_folder, feature_col=feature_col)

        self.plot_custom(df, split_folder, folder)

    @staticmethod
    def plot_barplot_attribute(df, split_folder, attribute, label=""):
        """
        Plots the distribution of the attribute of the decisions in the given dataframe
        :param df:              the dataframe containing the legal areas
        :param split_folder:    where to save the plots and csv files
        :param attribute:       the attribute to barplot
        :param label:           defines if only critical data of a label is considered
        :return:
        """
        attribute_df = df[attribute].value_counts().to_frame()
        total = len(df.index)
        # we deleted the ones where we did not find any attribute: also mention them in this table
        uncategorized = total - attribute_df[attribute].sum()
        attribute_df = attribute_df.reset_index(level=0)
        attribute_df = attribute_df.rename(columns={'index': attribute, attribute: 'number of decisions'})
        attribute_df['number of decisions'] = attribute_df['number of decisions'].astype(int)
        attribute_df.sort_values(by=[attribute], inplace=True)
        attribute_df.loc[len(attribute_df.index)] = ['uncategorized', uncategorized]
        attribute_df.loc[len(attribute_df.index)] = ['all', total]
        attribute_df['percent'] = round(attribute_df['number of decisions'] / total, 4)

        attribute_df.to_csv(split_folder / f'{attribute}_{label}_distribution.csv')
        # need to make sure to use right type
        attribute_df = attribute_df[~attribute_df[attribute].astype(str).str.contains('all')]
        fig = px.bar(attribute_df, x=attribute, y="number of decisions",
                     title=f'{attribute}_{label}_distribution-histogram')
        fig.write_image(split_folder / f'{attribute}_{label}_distribution-histogram.png')
        plt.close()

    @staticmethod
    def plot_labels(df, split_folder, label_name='label'):
        """
        Plots the label distribution of the decisions in the given dataframe
        :param df:              the dataframe containing the labels
        :param split_folder:    where to save the plots and csv files
        :param label_name:      name of the original label
        :return:
        """
        # compute label imbalance
        # ax = df.label.astype(str).hist()
        # ax.tick_params(labelrotation=30)
        # ax.get_figure().savefig(split_folder / 'multi_label_distribution.png', bbox_inches="tight")

        counter_dict = dict(Counter(np.hstack(df.label)))
        counter_dict['all'] = sum(counter_dict.values())
        label_counts = pd.DataFrame.from_dict(counter_dict, orient='index', columns=['num_occurrences'])
        label_counts.loc[:, 'percent'] = round(label_counts['num_occurrences'] / counter_dict['all'], 4)
        label_counts.to_csv(split_folder / f"{label_name}_distribution.csv", index_label='label')

        ax = label_counts[~label_counts.index.str.contains("all")].plot.bar(y='num_occurrences', rot=15)
        ax.get_figure().savefig(split_folder / f"{label_name}_distribution.png", bbox_inches="tight")
        plt.clf()

    @staticmethod
    def plot_input_length(df, split_folder, feature_col='full_text'):
        """
        Plots the input length of the decisions in the given dataframe
        :param df:              the dataframe containing the decision texts
        :param split_folder:    where to save the plots and csv files
        :param feature_col:     spezifies feature_col
        :return:
        """
        # compute median input length
        input_length_distribution = df.loc[:, ['num_tokens_spacy', 'num_tokens_bert']].describe().round(0).astype(int)
        input_length_distribution.to_csv(split_folder / f'{feature_col}_input_length_distribution.csv',
                                         index_label='measure')

        # bin outliers together at the cutoff point
        cutoff = 4000
        cut_df = df.loc[:, ['num_tokens_spacy', 'num_tokens_bert']]
        cut_df.num_tokens_spacy = cut_df.num_tokens_spacy.clip(upper=cutoff)
        cut_df.num_tokens_bert = cut_df.num_tokens_bert.clip(upper=cutoff)

        hist_df = pd.concat([cut_df.num_tokens_spacy, cut_df.num_tokens_bert], keys=['spacy', 'bert']).to_frame()
        hist_df = hist_df.reset_index(level=0)
        hist_df = hist_df.rename(columns={'level_0': 'tokenizer', 0: 'Number of tokens'})

        plot = sns.displot(hist_df, x="Number of tokens", hue="tokenizer",
                           bins=100, kde=True, fill=True, height=5, aspect=2.5, legend=False)
        plot.set(xticks=list(range(0, 4500, 500)))
        plt.ylabel('Number of court cases')
        plt.legend(["BERT", "SpaCy"], loc='upper right', title='Tokenizer', fontsize=16, title_fontsize=18)
        plot.savefig(split_folder / f'{feature_col}_input_length_distribution-histogram.png', bbox_inches="tight")
        plt.clf()

        plot = sns.displot(hist_df, x="Number of tokens", hue="tokenizer", kind="ecdf", legend=False)
        plt.ylabel('Number of court cases')
        plt.legend(["BERT", "SPaCy"], loc='lower right', title='Tokenizer')
        plot.savefig(split_folder / f'{feature_col}_input_length_distribution-cumulative.png', bbox_inches="tight")
        plt.clf()

        plot = sns.displot(cut_df, x="num_tokens_spacy", y="num_tokens_bert")
        plot.savefig(split_folder / f'{feature_col}_input_length_distribution-bivariate.png', bbox_inches="tight")
        plt.clf()

    def save_labels(self, labels, folder):
        """
        Saves the labels and the corresponding ids as a json file
        :param labels:      list of labels dict
        :param folder:      where to save the labels
        :return:
        """
        if labels:  # labels can also be None (for PretrainingDatasetCreator), in which case we do nothing
            assert len(labels) <= 2
            i = 1
            for entry in labels:
                entry = list(entry)
                labels_dict = dict(enumerate(entry))
                json_labels = {"id2label": labels_dict, "label2id": {y: x for x, y in labels_dict.items()}}
                if len(labels) != 1:
                    file_name = folder / f"labels_{i}.json"
                    i = i + 1
                else:
                    file_name = folder / "labels.json"
                with open(f"{file_name}", 'w', encoding='utf-8') as f:
                    json.dump(json_labels, f, ensure_ascii=False, indent=4)
        else:
            self.logger.info("No labels given.")

    def split_date_stratified(self, dataset, start_years: dict):
        """
        Splits the dataset into train, val and test based on the date
        :param dataset:            the dataset to be split
        :param start_years:   the years when to start each split
        :return:
        """
        # TODO revise this for datasets including cantonal data and include year 2021
        train = dataset.filter(lambda x: x["year"] in range(start_years["train"], start_years["validation"]))
        val = dataset.filter(lambda x: x["year"] in range(start_years["validation"], start_years["test"]))
        test = dataset.filter(lambda x: x["year"] in range(start_years["test"], start_years["secret_test"]))
        secret_test = dataset.filter(lambda x: x["year"] in range(start_years["secret_test"], self.current_year + 1))

        return train, val, test, secret_test

    def split_random(self, dataset):
        """
        Splits the dataset randomly into train, val and test
        :param dataset:      the dataset to be split
        :return:
        """
        # 80% train, 20% test + validation
        train_testvalid = dataset.train_test_split(test=0.2)
        # Split the 20% test + valid in half test, half valid
        test_valid = train_testvalid['test'].train_test_split(test=0.5)
        # gather everything into a single DatasetDict
        return train_testvalid['train'], test_valid['train'], test_valid['test']

    @abc.abstractmethod
    def plot_custom(self, df, split_folder, folder):
        """
        Implement custom plots for each dataset_creator in this method
        """
        raise NotImplementedError("This method should be implemented in the subclass.")
