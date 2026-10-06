"""MyTreeTags: numeric tag ids (HAR c60/c61, ids learned by toggling every tag button on a throwaway person) and
name -> id resolution. Names match case-, space- and punctuation-insensitively; unknown names are rejected; raw ids pass."""
from __future__ import annotations
import re

# UI button name (CamelCase, as in the page) -> numeric id
TAG_IDS = {
    "ActivelyResearching": "3",
    "AdoptedIntoThisFamily": "1",
    "AdoptedOutofThisFamily": "2",
    "BirthDocumented": "85",
    "Blind": "44",
    "BrickWall": "4",
    "BurialDocumented": "91",
    "CivilWarConfederateSoldier": "53",
    "CivilWarEra": "66",
    "CivilWarSoldier": "52",
    "CivilWarUnionSoldier": "54",
    "CommonDNAAncestor": "5",
    "Complete": "6",
    "CompleteCensusRecords": "93",
    "DNAConnection": "9",
    "DNAMatch": "10",
    "Deaf": "45",
    "DeathDocumented": "89",
    "DiedInInfancy": "39",
    "DiedYoung": "7",
    "DiedinAccident": "42",
    "DiedinEpidemic": "41",
    "DiedinWar": "43",
    "DirectAncestor": "8",
    "EconomicMobility": "60",
    "Employer": "61",
    "EnslavedPerson": "11",
    "FreePersonofColor": "12",
    "GoldenWeddingAnniv": "65",
    "GreatDepression": "68",
    "Hypothesis": "13",
    "Immigrant": "14",
    "IndenturedServant": "15",
    "KoreanWarSoldier": "58",
    "LargeFamily8children": "63",
    "LiteracyAcquisition": "59",
    "Literate": "48",
    "LivedinOneRegion": "47",
    "LongMarriage": "62",
    "MarriageDocumented": "87",
    "MarriedMultipleTimes": "40",
    "MilitaryService": "16",
    "MinimalDocumentation": "80",
    "MissingBirthRecord": "86",
    "MissingBurialEvent": "92",
    "MissingCensusRecords": "94",
    "MissingDeathRecord": "90",
    "MissingMarriageRecord": "88",
    "MultipleSpouses": "17",
    "NeverMarried": "18",
    "NoChildren": "19",
    "NoDocumentation": "81",
    "OriginalSources": "82",
    "Orphan": "20",
    "PartiallyDocumented": "79",
    "PrioritizedPerson": "32",
    "Profession": "163185",
    "PropertyOwner": "49",
    "ReconstructionEra": "71",
    "RevolutionarySoldier": "50",
    "RevolutionaryWarEra": "67",
    "RoyaltyNobility": "21",
    "SecondarySource": "84",
    "SilverWeddingAnniv": "64",
    "SingleSource": "83",
    "SlaveOwner": "22",
    "ToDo": "33",
    "USColoredTroops": "55",
    "Unverified": "23",
    "Verified": "24",
    "WWIEra": "69",
    "WWIIEra": "70",
    "WWIISoldier": "57",
    "WWISoldier": "56",
    "Warof1812Soldier": "51",
    "Well-documented": "78",
    "Widow": "72",
    "Widower": "73",
    "iMemories": "95",
}
_BY_KEY = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in TAG_IDS.items()}


def resolve_tags(value):
    """'To Do,34' / ['to-do', 34] -> ['33', '34']."""
    from .ops import WriteRequestError
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    out = []
    for item in (str(i).strip() for i in items):
        key = re.sub(r"[^a-z0-9]", "", item.lower())
        if re.fullmatch(r"\d{1,6}", item):
            out.append(item)
        elif key in _BY_KEY:
            out.append(_BY_KEY[key])
        else:
            from .runtime import suggest
            close = suggest(key, list(_BY_KEY))
            raise WriteRequestError("unknown-tag", [{"field": "tags", "issue": "unknown-value",
                                                     "did_you_mean": [n for n in TAG_IDS if re.sub(r"[^a-z0-9]", "", n.lower()) in close]}])
    if not out or len(out) > 20:
        raise WriteRequestError("invalid-write-request")
    return out
