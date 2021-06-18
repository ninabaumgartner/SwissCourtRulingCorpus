import unicodedata
from typing import Any, Optional, Tuple, List, Dict

import bs4
import re

from scrc.dataset_construction.section_splitter import sections
from scrc.utils.main_utils import clean_text

"""
This file is used to extract sections from decisions sorted by spiders.
The name of the functions should be equal to the spider! Otherwise, they won't be invocated!
"""

def CH_BGer(soup: Any, namespace: dict) -> Optional[Tuple[dict, List[Dict[str, str]]]]:
    """
    IMPORTANT: So far, only German is supported!
    :param soup:        the soup parsed by bs4
    :param namespace:   the namespace containing some metadata of the court decision
    :return:            the sections dict, None if not in German
    """
    if namespace['language'] != 'de':
        raise ValueError("This function is only implemented for the German language so far.")

    # As soon as one of the strings in the list (value) is encountered we switch to the corresponding section (key)
    section_markers = {
        # "header" has no markers!
        # at some later point we can still divide rubrum into more fine-grained sections like title, judges, parties, topic
        # "title": ['Urteil vom', 'Beschluss vom', 'Entscheid vom'],
        # "judges": ['Besetzung', 'Es wirken mit', 'Bundesrichter'],
        # "parties": ['Parteien', 'Verfahrensbeteiligte', 'In Sachen'],
        # "topic": ['Gegenstand', 'betreffend'],
        "facts": [r'Sachverhalt:', r'hat sich ergeben', r'Nach Einsicht'],
        "considerations": [r'Erwägung:', r'in Erwägung', r'Erwägungen:'],
        "rulings": [r'erkennt die Präsidentin', r'erkennt der Präsident', r'Demnach erkennt', r'beschliesst:'],
        "footer": [
            r'\w*,\s\d?\d\.\s(?:Jan(?:uar)?|Feb(?:ruar)?|Mär(?:z)?|Apr(?:il)?|Mai|Jun(?:i)?|Jul(?:i)?|Aug(?:ust)?|Sep(?:tember)?|Okt(?:ober)?|Nov(?:ember)?|Dez(?:ember)?).*']
    }
    # normalize strings to avoid problems with umlauts
    for key, value in section_markers.items():
        section_markers[key] = [unicodedata.normalize('NFC', marker) for marker in value]

    def get_paragraphs(soup):
        """
        Get Paragraphs in the decision
        :param soup:
        :return:
        """
        divs = soup.find_all("div", class_="content")
        assert len(divs) <= 2  # we expect maximally two divs with class content

        paragraphs = []
        heading, paragraph = None, None
        for element in divs[0]:
            if isinstance(element, bs4.element.Tag):
                text = str(element.string)
                # This is a hack to also get tags which contain other tags such as links to BGEs
                if text.strip() == 'None':
                    text = element.get_text()
                if "." in text and len(text) < 5:  # get numerated titles such as 1. or A.
                    heading = text  # set heading for the next paragraph
                else:
                    if heading is not None:  # if we have a heading
                        paragraph = heading + " " + text  # add heading to text of the next paragraph
                    else:
                        paragraph = text
                    heading = None  # reset heading
                paragraph = clean_text(paragraph)
                if paragraph not in ['', ' ', None]:  # discard empty paragraphs
                    paragraphs.append(paragraph)
        return paragraphs

    def associate_sections(paragraphs, section_markers):
        paragraph_data = []
        section_data = {key: "" for key in sections}
        current_section = "header"
        for paragraph in paragraphs:
            # update the current section if it changed
            current_section = update_section(current_section, paragraph, section_markers)

            # construct the list of sections with associated text
            section_data[current_section] += paragraph + " "

            # construct the list of annotated paragraphs (can be used for prodigy annotation
            paragraph_data.append({"text": paragraph, "section": current_section})

        if current_section != 'footer':
            message = f"We got stuck at section {current_section}. Please check! " \
                      f"Here you have the url to the decision: {namespace['html_url']}"
            raise ValueError(message)
        return section_data, paragraph_data

    def update_section(current_section, paragraph, section_markers):
        if current_section == 'footer':
            return current_section  # we made it to the end, hooray!
        next_section_index = sections.index(current_section) + 1
        next_sections = sections[next_section_index:]  # consider all following sections
        for next_section in next_sections:
            markers = section_markers[next_section]
            paragraph = unicodedata.normalize('NFC', paragraph)
            for marker in markers:  # check each marker in the list
                if re.search(marker, paragraph):
                    return next_section  # change to the next section
        return current_section  # stay at the old section

    paragraphs = get_paragraphs(soup)
    section_data, paragraph_data = associate_sections(paragraphs, section_markers)
    return section_data, paragraph_data

# This needs special care
# def CH_BGE(soup: Any, namespace: dict) -> Optional[dict]:
#    return CH_BGer(soup, namespace)