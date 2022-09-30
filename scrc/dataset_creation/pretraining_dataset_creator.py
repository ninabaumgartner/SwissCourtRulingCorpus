import pandas as pd

from scrc.dataset_creation.dataset_creator import DatasetCreator
from scrc.enums.section import Section
from scrc.utils.log_utils import get_logger

from scrc.utils.main_utils import get_config


class PretrainingDatasetCreator(DatasetCreator):
    """
    Creates a dataset with all the full_text
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.logger = get_logger(__name__)

        self.debug = True
        self.overwrite_cache = True
        self.split_type = "all_train"
        self.dataset_name = "swiss_caselaw"
        self.feature_cols = [Section.FULL_TEXT]

    def prepare_dataset(self, save_reports):
        engine = self.get_engine(self.db_scrc)

        court_strings = next(self.select(engine, "court", "court_string", None))["court_string"].tolist()

        # df = self.get_df(engine, court_string="BL_EG", overwrite_cache=self.overwrite_cache)

        dfs = []
        for court_string in court_strings:
            # we don't use the cache since it is overwritten after each court
            df = self.get_df(engine, court_string=court_string, use_cache=False)
            if not df.empty:
                dfs.append(df)

        df = pd.concat(dfs)

        return df, None


if __name__ == '__main__':
    config = get_config()

    pretraining_dataset_creator = PretrainingDatasetCreator(config)
    pretraining_dataset_creator.create_dataset(sub_datasets=False, kaggle=False, huggingface=True, save_reports=False)