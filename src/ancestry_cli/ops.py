"""The write operations: one registry that says what each operation is, how its request is built, and what counts as
success.

Each operation is declared once with `@operation(...)`: its summary, required/optional fields, risk class, how it is
undone, and a success test. Request shapes come from HAR captures of the Ancestry web app (see ENDPOINT-MAP.md).

* `build(op, ...)`   offline request builder (no network).
* `succeeded(...)`   did the response mean the write happened? HTTP 2xx alone is never enough.
* `write(...)`       the entry point: dry-run by default; `--live` runs the guards, then `sender.send`.
* `OPS` / `SPECS`    the registry, used by the CLI choices, `ancestry ops`, and the generated docs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config
from .config import needs_confirmation

_PREFIX = "/family-tree/person"
_ID = re.compile(r"[1-9][0-9]{0,15}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_RELATIONS = ("Father", "Mother", "Spouse", "Son", "Daughter", "Brother", "Sister")
_FACT_KEYS = ("date", "description", "eventType", "gender", "label", "location", "name", "preferred",
              "showMap", "showOnLifeStory", "title")

# risk classes: how much an operation can change.
#   additive    adds content only                         edit       changes existing content (restorable)
#   structural  adds a person or relationship             destructive removes content (restorable only via undo)
#   hint-state  moves a hint between New/Undecided/Ignored/Rejected
ADDITIVE, EDIT, STRUCTURAL, DESTRUCTIVE, HINT_STATE = "additive", "edit", "structural", "destructive", "hint-state"


class WriteRequestError(ValueError):
    """A request that cannot be built. `code` is the error classification; `problems` says which fields are wrong:
    [{field, issue: missing|invalid|not-allowed|unknown-value, expected?, valid_values?, did_you_mean?}]. Never values."""
    def __init__(self, code="invalid-write-request", problems=None):
        super().__init__(code)
        self.code = code
        self.problems = problems or []


def _need(condition, field=None, issue="invalid", **extra):
    if not condition:
        raise WriteRequestError("invalid-write-request", [{"field": field, "issue": issue, **extra}] if field else None)


@dataclass
class Ctx:
    """The target of one write, handed to every request builder."""
    tree_id: int
    person_id: int
    actor: str = "{actor}"            # the account's user id; a placeholder in dry-runs
    assertion_id: object = None

    @property
    def person(self):
        return f"tree/{self.tree_id}/person/{self.person_id}"

    def fact(self, tail):
        return f"{_PREFIX}/factedit/user/{self.actor}/{self.person}/assertion/{tail}"


@dataclass
class Spec:
    name: str
    summary: str
    risk: str
    required: tuple = ()
    optional: tuple = ()
    undo: str = ""
    example: str = ""
    builder: object = None
    success: object = None
    notes: str = ""
    choices: dict = field(default_factory=dict)       # field -> the only allowed values (a fixed, public vocabulary)

    def describe(self):
        return {"op": self.name, "summary": self.summary, "risk": self.risk, "required": list(self.required),
                "optional": list(self.optional), "undo": self.undo, "example": self.example, "notes": self.notes}


SPECS: dict = {}


def operation(name, *, summary, risk, required=(), optional=(), undo="", example="", success, notes="", choices=None):
    """Register `builder(ctx, f)` as the request builder for `name`."""
    def register(builder):
        SPECS[name] = Spec(name, summary, risk, tuple(required), tuple(optional), undo, example, builder, success, notes,
                           choices or {})
        return builder
    return register


def _is(d, **expect):
    return isinstance(d, dict) and all(d.get(k) == v for k, v in expect.items())


# ---------------------------------------------------------------------------------------------------- facts
def _fact_body(assertion_id, f):
    _need(set(f) <= set(_FACT_KEYS))
    location = f.get("location", "")
    _need(isinstance(location, (str, dict)))        # add posts a bare string; edit posts {placeName, GPID, showUnderline}
    name = {"givenName": "", "surname": "", "suffix": "", **f.get("name", {})}
    _need(set(name) == {"givenName", "surname", "suffix"})
    body = {"assertionId": str(assertion_id), "date": f.get("date", ""), "description": f.get("description", ""),
            "eventType": f["eventType"], "gender": f.get("gender", ""), "location": location, "name": name,
            "preferred": None, "showMap": bool(f.get("showMap", False)),
            "showOnLifeStory": bool(f.get("showOnLifeStory", True)), "title": f.get("title", "")}
    if str(f["eventType"]).replace(" ", "").lower() == "customevent":      # the site wants its own id spelling and the label
        _need(isinstance(f.get("label"), str) and f["label"].strip(), "label", "missing")
        body["eventType"], body["customEventTitle"] = "customevent", f["label"].strip()
    elif f.get("label"):
        raise WriteRequestError("invalid-write-request", [{"field": "label", "issue": "not-allowed"}])
    return body


_FACT_FIELDS = ("date", "description", "location", "gender", "title", "name", "showMap", "showOnLifeStory", "label")


@operation("fact-add", summary="Add a fact or event (birth, residence, ...) to a person.", risk=ADDITIVE,
           required=("eventType",), optional=_FACT_FIELDS, undo="journal undo removes the fact",
           example="--set eventType=Residence --set date=1900 --set description='Lived on Main St'",
           notes="eventType CustomEvent needs --set label='the fact label' (the title the site shows); description is optional.",
           success=lambda d: _is(d, status=True))
def _fact_add(c, f):
    _need("eventType" in f)
    return dict(method="POST", path=c.fact("0/save"), body=_fact_body(0, f))


@operation("fact-edit", summary="Change a fact. Only the fields you give change; the rest keep their current values.",
           risk=EDIT, required=("--assertion",), optional=("eventType",) + _FACT_FIELDS,
           undo="journal undo restores the previous values (from the before-snapshot)",
           example="--assertion 700000000001 --set date=1901",
           notes="A person's name is edited as a fact with eventType=Name.", success=lambda d: _is(d, status=True))
def _fact_edit(c, f):
    _need(_ID.match(str(c.assertion_id)) and "eventType" in f)
    return dict(method="POST", path=c.fact(f"{c.assertion_id}/save"), body=_fact_body(c.assertion_id, f))


@operation("fact-remove", summary="Delete a fact.", risk=DESTRUCTIVE, required=("--assertion",),
           undo="journal undo recreates the fact (new id; its citations are not re-attached)",
           example="--assertion 700000000001", success=lambda d: d == [True])
def _fact_remove(c, f):
    _need(_ID.match(str(c.assertion_id)))
    return dict(method="GET", path=c.fact(f"{c.assertion_id}/delete"), body=None)       # a side-effecting GET


# ---------------------------------------------------------------------------------------------------- people
@operation("relative-add", summary="Create a new person and link them to this person as a relative.", risk=STRUCTURAL,
           required=("relation", "status"), optional=("given", "surname", "suffix", "gender"),
           choices={"relation": _RELATIONS, "status": ("Living", "Deceased"), "gender": ("Male", "Female", "Unknown")},
           undo="journal undo removes the new person",
           example="--set relation=Father --set given=John --set surname=Doe --set gender=Male --set status=Deceased",
           notes="relation: Father, Mother, Spouse (verified), Son, Daughter, Brother, Sister (same route, unverified). "
                 "status is Living or Deceased and has no default: a wrong guess could expose a living person. "
                 "Always creates a NEW person; use relative-link for someone already in the tree.",
           success=lambda d: isinstance(d, dict) and bool(d.get("newPid")))
def _relative_add(c, f):
    _need(f.get("relation") in _RELATIONS and f.get("name_id") and f.get("gender_id") and f.get("status") in ("Living", "Deceased"))
    # nameId/genderId are issued by the server (GET .../add?rel=<relation>); the sender fetches them before this runs
    return dict(method="POST", path=f"{_PREFIX}/addedit/user/{c.actor}/{c.person}/addperson",
                body={"addTarget": None,
                      "person": {"personId": str(c.person_id), "treeId": str(c.tree_id), "userId": c.actor,
                                 "gender": f.get("anchor_gender", "")},
                      "type": f["relation"],
                      "values": {"": "", "radioTab": "New person", "fname": f.get("given", ""), "lname": f.get("surname", ""),
                                 "sufname": f.get("suffix", ""), "genderRadio": f.get("gender", ""), "statusRadio": f["status"],
                                 "bdate": "", "bplace": "", "ddate": "", "dplace": "", "isAlternateParent": False,
                                 "nameId": str(f["name_id"]), "genderId": str(f["gender_id"])}})


@operation("relative-link", summary="Link a person who is already in the tree as a relative of this person.", risk=STRUCTURAL,
           required=("relation", "existing_person_id"), optional=("name",), choices={"relation": _RELATIONS},
           undo="not undoable: no relationship-only removal route is known; remove the relationship in the Ancestry UI",
           example="--set relation=Father --set existing_person_id=100000000001",
           notes="Find the person first with `find --complete`. The relationship is the same kind the UI's \"From your tree\" option "
                 "creates. Check the result with `ancestry person`: the response body does not confirm the link.",
           success=lambda d: isinstance(d, dict) and not d.get("ErrorCode"))
def _relative_link(c, f):
    _need(f.get("relation") in _RELATIONS and _ID.match(str(f.get("existing_person_id", ""))), "existing_person_id")
    _need(str(f["existing_person_id"]) != str(c.person_id), "existing_person_id", "invalid")
    return dict(method="POST", path=f"{_PREFIX}/addedit/user/{c.actor}/{c.person}/addperson",
                body={"person": {"personId": str(c.person_id), "treeId": str(c.tree_id), "userId": c.actor},
                      "type": f["relation"],
                      "values": {"apmFindExistingPerson": {"name": f.get("name", ""), "birth": "", "death": "",
                                                           "PID": int(f["existing_person_id"]), "genderIconType": ""},
                                 "attachedChildren": []}})


@operation("person-remove", summary="Permanently delete a person from the tree.", risk=DESTRUCTIVE, optional=("name",),
           undo="not undoable; a before-snapshot is saved so the person can be recreated by hand",
           example="(no fields needed)",
           notes="The display name is read from the page when sent; you do not need to supply it.",
           success=lambda d: _is(d, success=True))
def _person_remove(c, f):
    _need(isinstance(f.get("name"), str) and f["name"])
    return dict(method="POST", path=f"{_PREFIX}/tree/{c.tree_id}/person/{c.person_id}/removePerson", body={"name": f["name"]})


# ---------------------------------------------------------------------------------------------------- sources
_SRC = {"author": "auth", "publisher": "pub", "publication_place": "publ", "publication_date": "pubd",
        "call_number": "cn", "refn": "refn", "note": "note"}           # field name -> the site's key
_CIT = {"date": "d", "other_info": "oi", "transcription": "trans"}
_REPO = {"address": "adr", "phone": "ph", "email": "eml", "call_number": "cn", "refn": "refn", "note": "note"}


def _src_body(title, f):
    return {"title": title, **{key: str(f.get(name) or "") for name, key in _SRC.items()},
            "repositoryId": str(f.get("repository_id") or "")}


def _cit_body(f):
    return {"title": f["title"], "url": f.get("url", ""), **{key: str(f.get(name) or "") for name, key in _CIT.items()},
            "sourceId": str(f["source_id"])}


@operation("source-create", summary="Create a custom source in the tree.", risk=ADDITIVE, required=("title",),
           optional=tuple(_SRC) + ("repository_id",), undo="`journal undo` deletes the source with source-delete",
           example="--set title='1900 census, Springfield IL' --set author='US Census Bureau' --set publication_date=1900",
           notes="Returns ids.gid, the new source id (use it as source_id). Any field beyond title is saved with a second request; "
                 "progress shows how far it got. author, publisher, publication_place, publication_date, call_number, refn, note; "
                 "repository_id links a repository from repository-create.",
           success=lambda d: isinstance(d, dict) and isinstance(d.get("gid"), dict))
def _source_create(c, f):
    _need(f.get("title"))
    req = dict(method="POST", path=f"{_PREFIX}/sourceedit/user/{c.actor}/tree/{c.tree_id}/source", body={"title": f["title"]})
    if any(f.get(k) for k in tuple(_SRC) + ("repository_id",)):
        req["then"] = dict(method="PUT", path=f"{_PREFIX}/sourceedit/user/{c.actor}/tree/{c.tree_id}/source/{{id}}",
                           body=_src_body(f["title"], f))
    return req


@operation("source-edit", summary="Change a source's fields. Fields you do not give keep their current values.", risk=EDIT,
           required=("source_id",), optional=("title",) + tuple(_SRC) + ("repository_id",), undo="not undoable",
           example="--set source_id=380000001 --set publisher='Government Printing Office'",
           notes="person is only the page used for the pre-flight check. Current values are read from the source list first.",
           success=lambda d: True)
def _source_edit(c, f):
    _need(_ID.match(str(f.get("source_id", ""))) and f.get("title"))
    return dict(method="PUT", path=f"{_PREFIX}/sourceedit/user/{c.actor}/tree/{c.tree_id}/source/{f['source_id']}",
                body=_src_body(f["title"], f))


@operation("repository-create", summary="Create a repository (archive, library, website) in the tree.", risk=ADDITIVE,
           required=("name",), optional=tuple(_REPO), undo="not undoable: remove it in the Ancestry UI",
           example="--set name='Ohio History Connection' --set address='800 E 17th Ave, Columbus OH'",
           notes="Returns ids.gid, the repository id (use it as repository_id).",
           success=lambda d: isinstance(d, dict) and isinstance(d.get("gid"), dict))
def _repository_create(c, f):
    _need(f.get("name"))
    return dict(method="POST", path=f"{_PREFIX}/sourceedit/user/{c.actor}/tree/{c.tree_id}/repository",
                body={"name": f["name"], **{key: str(f.get(name) or "") for name, key in _REPO.items()}})


@operation("source-set-repository", summary="Link a repository to a source.", risk=EDIT, required=("source_id", "repository_id"),
           undo="not undoable", example="--set source_id=380000001 --set repository_id=250000001", success=lambda d: True,
           notes="person is only the page used for the pre-flight check.")
def _source_set_repository(c, f):
    _need(_ID.match(str(f.get("source_id", ""))) and _ID.match(str(f.get("repository_id", ""))))
    return dict(method="PUT", path=f"{_PREFIX}/sourceedit/user/{c.actor}/tree/{c.tree_id}/source/{f['source_id']}/reference",
                body={"repositoryId": str(f["repository_id"]), "actionType": "attach"})


@operation("source-delete", summary="Permanently delete a custom source, and every citation of it, from the tree.", risk=DESTRUCTIVE,
           required=("source_id",), optional=("expect_title",), undo="not undoable",
           example="--set source_id=380000001 --set expect_title='1900 census, Springfield IL'",
           notes="expect_title is required unless this tool created the source (the journal knows it); the live delete is refused if the "
                 "source's title differs. Citations of the source disappear from every person. person is only the page used for the pre-flight check.",
           success=lambda d: d == {})
def _source_delete(c, f):
    _need(_ID.match(str(f.get("source_id", ""))))
    return dict(method="DELETE", path=f"{_PREFIX}/sourceedit/user/{c.actor}/tree/{c.tree_id}/source/{f['source_id']}", body=None)


@operation("citation-add", summary="Cite an existing source on a person.", risk=ADDITIVE, required=("title", "source_id"),
           optional=("url",) + tuple(_CIT), undo="journal undo is not available; use citation-remove",
           example="--set source_id=380000001 --set title='page 12, line 4' --set date='1 Jan 1900'",
           notes="title is the citation detail text. date, other_info and transcription are saved with a second request. "
                 "Returns ids.gid, the new citation id.",
           success=lambda d: isinstance(d, dict) and isinstance(d.get("gid"), dict))
def _citation_add(c, f):
    _need(f.get("title") and _ID.match(str(f.get("source_id", ""))))
    req = dict(method="POST", path=f"{_PREFIX}/sourceedit/user/{c.actor}/{c.person}/citation",
               body={"title": f["title"], "url": f.get("url", ""), "sourceId": str(f["source_id"])})
    if any(f.get(k) for k in _CIT):
        req["then"] = dict(method="PUT", path=f"{_PREFIX}/sourceedit/user/{c.actor}/{c.person}/citation/{{id}}", body=_cit_body(f))
    return req


@operation("citation-edit", summary="Change a citation: details, web address, date, other information, transcription.", risk=EDIT,
           required=("citation_id", "source_id", "title"), optional=("url",) + tuple(_CIT), undo="not undoable",
           example="--set citation_id=600000000001 --set source_id=380000001 --set title='page 12' --set transcription='...'",
           notes="Replaces every citation field: fields you leave out are cleared, so give them all.",
           success=lambda d: True)
def _citation_edit(c, f):
    _need(f.get("title") and _ID.match(str(f.get("source_id", ""))) and _ID.match(str(f.get("citation_id", ""))))
    return dict(method="PUT", path=f"{_PREFIX}/sourceedit/user/{c.actor}/{c.person}/citation/{f['citation_id']}", body=_cit_body(f))


@operation("citation-remove", summary="Remove a citation from a person.", risk=DESTRUCTIVE, required=("citation_id",),
           undo="journal undo is not available", example="--set citation_id=600000000001",
           success=lambda d: _is(d, result=True))
def _citation_remove(c, f):
    _need(_ID.match(str(f.get("citation_id", ""))))
    return dict(method="POST", path=f"{_PREFIX}/factedit/user/{c.actor}/{c.person}/citation/{f['citation_id']}/removecitation", body=None)


def _attach_body(f):
    _need(_ID.match(str(f.get("citation_id", ""))))
    return {"databaseId": "", "recordId": "", "sourceCitationId": str(f["citation_id"])}   # empty ids = a custom source


@operation("fact-attach-source", summary="Attach an existing citation to a fact.", risk=ADDITIVE,
           required=("--assertion", "citation_id"), undo="journal undo detaches it",
           example="--assertion 700000000001 --set citation_id=600000000001", success=lambda d: _is(d, ErrorCode=0))
def _attach(c, f):
    _need(_ID.match(str(c.assertion_id)))
    return dict(method="POST", path=c.fact(f"{c.assertion_id}/attachSource"), body=_attach_body(f))


@operation("fact-detach-source", summary="Detach a citation from a fact.", risk=EDIT, required=("--assertion", "citation_id"),
           undo="journal undo is not available; use fact-attach-source", example="--assertion 700000000001 --set citation_id=600000000001",
           success=lambda d: d == {} or _is(d, ErrorCode=0))
def _detach(c, f):
    _need(_ID.match(str(c.assertion_id)))
    return dict(method="POST", path=c.fact(f"{c.assertion_id}/detachSource"), body=_attach_body(f))


# ---------------------------------------------------------------------------------------------------- links, notes, tags, media
@operation("weblink-add", summary="Add a web link to a person.", risk=ADDITIVE, required=("href", "title"),
           undo="journal undo removes the link", example="--set href=https://example.com/page --set title='Obituary'",
           notes="Returns ids.webLinkId.", success=lambda d: isinstance(d, dict) and isinstance(d.get("result"), list))
def _weblink_add(c, f):
    _need(f.get("href") and f.get("title"))
    return dict(method="POST", path=f"{_PREFIX}/facts/user/{c.actor}/{c.person}/weblinkadd",
                body={"webLinkHref": f["href"], "webLinkTitle": f["title"]})


@operation("weblink-remove", summary="Remove a web link from a person.", risk=DESTRUCTIVE, required=("web_link_id",),
           undo="journal undo is not available", example="--set web_link_id=00000000-0000-4000-8000-000000000001",
           success=lambda d: isinstance(d, dict) and isinstance(d.get("result"), list))
def _weblink_remove(c, f):
    _need(f.get("web_link_id"))
    return dict(method="GET", path=f"{_PREFIX}/facts/user/{c.actor}/{c.person}/weblinkremove", body=None,
                query={"webLinkId": f["web_link_id"]})


@operation("note-set", summary="Set the person's shared note (replaces the whole note; empty text clears it).", risk=EDIT,
           required=("text",), undo="journal undo restores the previous note", example="--set 'text=Needs a source for the 1900 census'",
           success=lambda d: isinstance(d, dict) and bool(d.get("id")) and "txt" in d)
def _note_set(c, f):
    text = f.get("text")
    _need(isinstance(text, str) and len(text) <= 20000)
    return dict(method="POST", path=f"{_PREFIX}/workspace/user/{c.actor}/{c.person}/savePersonNotes", body={"note": text})


@operation("tag-add", summary="Add MyTreeTags to a person (by name or numeric id).", risk=ADDITIVE, required=("tags",),
           undo="journal undo removes the same tags", example="--set 'tags=To Do,Brick Wall'",
           notes="Names are case/space-insensitive; 79 tags are known (see ancestry/tags.py).",
           success=lambda d: _is(d, statusCode=200) and bool(d.get("addedTags")))
def _tag_add(c, f):
    from .tags import resolve_tags
    return dict(method="POST", path=f"{_PREFIX}/workspace/user/{c.actor}/{c.person}/addtags", body={"tagnames": resolve_tags(f.get("tags"))})


@operation("tag-remove", summary="Remove MyTreeTags from a person.", risk=DESTRUCTIVE, required=("tags",),
           undo="journal undo re-adds the same tags", example="--set 'tags=To Do'",
           success=lambda d: _is(d, statusCode=200) and bool(d.get("removedTags")))
def _tag_remove(c, f):
    from .tags import resolve_tags
    return dict(method="POST", path=f"{_PREFIX}/workspace/user/{c.actor}/{c.person}/removetags", body={"tagnames": resolve_tags(f.get("tags"))})


@operation("media-upload", summary="Upload an image and attach it to a person.", risk=ADDITIVE, required=("file", "title"),
           undo="journal undo deletes the media", example="--set file=/abs/photo.jpg --set 'title=Wedding, 1950'",
           notes="png, jpg, gif or webp up to 25 MB; the journal records its sha256, size and file name.",
           success=lambda d: isinstance(d, dict) and bool(d.get("attaches")) and bool(d["attaches"][0].get("treeMediaId")))
def _media_upload(c, f):
    from .media import media_upload_plan
    return media_upload_plan(c.tree_id, c.person_id, f.get("file"), f.get("title"))


@operation("media-remove", summary="Permanently delete a media item from the tree.", risk=DESTRUCTIVE, required=("media_id",),
           undo="not undoable", example="--set media_id=00000000-0000-4000-8000-000000000002", success=lambda d: d == {})
def _media_remove(c, f):
    _need(_UUID.match(str(f.get("media_id", ""))))
    return dict(method="DELETE", path=f"/api/media/viewer/api/trees/{c.tree_id}/media/{f['media_id']}", body=None)


# ---------------------------------------------------------------------------------------------------- hints
def _hint_state(name, state, summary, undo):
    @operation(name, summary=summary, risk=HINT_STATE, required=("hint_id",), undo=undo, example="--set hint_id=100000000001",
               success=lambda d: d == {} or _is(d, ErrorCode=0))
    def build(c, f):
        _need(_ID.match(str(f.get("hint_id", ""))))
        return dict(method="PATCH", path=f"/api/hintsui-api/trees/{c.tree_id}/persons/{c.person_id}/hints/{f['hint_id']}",
                    body={"state": state}, text_plain=True)
    return build


_hint_state("hint-maybe", "deferred", "Mark a hint Maybe (Undecided).", "journal undo sets it back to New (hint-new)")
_hint_state("hint-no", "rejected", "Mark a hint No (rejected).", "journal undo sets it back to New (hint-new)")
_hint_state("hint-new", "pending", "Put a hint back to New (also un-accepts an accepted hint's state).", "hint-maybe or hint-no")


def _hint_query(c, f):
    _need(_ID.match(str(f.get("hint_id", ""))))
    return {"treeId": str(c.tree_id), "personId": str(c.person_id), "hintId": str(f["hint_id"]),
            "suppressConfirmation": "true", "bePage": "www.ancestry.com"}


@operation("hint-ignore", summary="Ignore a hint.", risk=HINT_STATE, required=("hint_id",), undo="journal undo restores it to Undecided (hint-restore)",
           example="--set hint_id=100000000001", success=lambda d: d == {} or _is(d, ErrorCode=0))
def _hint_ignore(c, f):
    return dict(method="POST", path="/hintsui-personhints/api/IgnoreHint", body=None, query=_hint_query(c, f))


@operation("hint-restore", summary="Move an ignored hint back to Undecided.", risk=HINT_STATE, required=("hint_id",),
           undo="hint-ignore", example="--set hint_id=100000000001", success=lambda d: d == {} or _is(d, ErrorCode=0),
           notes="Restores to Undecided, not New; use hint-new for New.")
def _hint_restore(c, f):
    return dict(method="POST", path="/hintsui-personhints/api/DeferHint", body=None, query=_hint_query(c, f))


# ---------------------------------------------------------------------------------------------------- public API
OPS = tuple(SPECS)


def validate(op, fields, assertion_id=None):
    """Every problem with the user's fields, found up front and reported together (never the values)."""
    from .runtime import suggest
    spec = SPECS[op]
    given = {**fields, **({"assertion_id": assertion_id} if assertion_id else {})}
    allowed = {r.lstrip("-") if r != "--assertion" else "assertion_id" for r in spec.required} | set(spec.optional) \
        | {"assertion_id", "confirm_tree", "force", "name_id", "gender_id", "anchor_gender"}
    problems = []
    for name in spec.required:
        key = "assertion_id" if name == "--assertion" else name
        if key not in given or (given[key] in (None, "") and key != "text"):      # note-set may clear a note with empty text
            problems.append({"field": "--assertion" if key == "assertion_id" else key, "issue": "missing",
                             **({"valid_values": list(spec.choices[key])} if key in spec.choices else {})})
    for key, value in given.items():
        if key not in allowed:
            problems.append({"field": key, "issue": "not-allowed", "did_you_mean": suggest(key, sorted(allowed - {"confirm_tree", "force"}))})
        elif key in spec.choices and value not in (None, "") and value not in spec.choices[key]:
            problems.append({"field": key, "issue": "unknown-value", "valid_values": list(spec.choices[key]),
                             "did_you_mean": suggest(value, spec.choices[key])})
        elif (key == "assertion_id" or key.endswith("_id")) and key not in ("name_id", "gender_id", "hint_id", "web_link_id", "media_id") \
                and value not in (None, "") and not _ID.match(str(value)):
            problems.append({"field": key, "issue": "invalid", "expected": "a positive integer id"})
    return problems


def build(op, *, tree_id, person_id, actor="{actor}", assertion_id=None, **f):
    """Return {method, path, body[, query, text_plain]} for one operation, or raise WriteRequestError."""
    _need(op in SPECS and _ID.match(str(tree_id)) and _ID.match(str(person_id)))
    return SPECS[op].builder(Ctx(tree_id, person_id, actor, assertion_id), f)


def succeeded(op, status, data):
    """Per-operation success test. The default is failure."""
    spec = SPECS.get(op)
    return bool(spec and isinstance(status, int) and 200 <= status < 300 and spec.success(data))


def _problem_result(problems):
    from .runtime import failure
    fields = ", ".join(sorted({p["field"] for p in problems if p.get("field")}))
    return failure("invalid-write-request", problems=problems,
                   message=f"{len(problems)} problem{'s' if len(problems) != 1 else ''} with the fields ({fields}). See `problems`.")


def write(*, op, tree_id, person_id, dry_run=True, confirm_tree=None, force=False, **fields):
    """Dry-run by default (shape only, no values echoed). With dry_run=False: allowlist, confirmation and duplicate
    guards, then `sender.send`. Every result names the `op`, `tree_id` and `person_id` it is about."""
    result = _write(op=op, tree_id=tree_id, person_id=person_id, dry_run=dry_run, confirm_tree=confirm_tree, force=force, **fields)
    if isinstance(result, dict):
        result.setdefault("op", op)
        result.setdefault("tree_id", tree_id)
        result.setdefault("person_id", person_id)
    return result


def _write(*, op, tree_id, person_id, dry_run, confirm_tree, force, **fields):
    from .runtime import failure, suggest
    if op not in SPECS:
        return failure("invalid-write-request", problems=[{"field": "op", "issue": "unknown-value", "valid_values": list(SPECS),
                                                           "did_you_mean": suggest(op, SPECS)}])
    if not config.tree_allowed(tree_id):
        return failure("configuration-error", problems=[{"field": "tree_id", "issue": "invalid", "expected": "a positive integer id"}])
    problems = validate(op, {k: v for k, v in fields.items() if k != "assertion_id"}, fields.get("assertion_id"))
    if problems:
        return _problem_result(problems)
    if dry_run is not True:
        from .journal import find_duplicate, request_hash
        from .sender import send
        if not config.writable(tree_id):
            return failure("tree-not-writable")
        if needs_confirmation(tree_id, confirm_tree):
            return failure("confirm-tree-required")
        req_hash = request_hash(op, tree_id, person_id, fields)
        duplicate = None if force else find_duplicate(req_hash)
        if duplicate is not None:       # an identical write is on record (ok or unknown): refuse a blind repeat
            return failure("duplicate-write", journal_id=duplicate)
        return send(op=op, tree_id=tree_id, person_id=person_id, req_hash=req_hash, **fields)
    # dry-run: fill what the live sender reads from the page, so the request shape can be shown
    placeholders = {"person-remove": {"name": "<read from the page>"}, "fact-edit": {"eventType": "<current value>"},
                    "relative-add": {"name_id": "0", "gender_id": "0"}}.get(op, {})
    fields = {**fields, **{k: v for k, v in placeholders.items() if not fields.get(k)}}
    try:
        req = build(op, tree_id=tree_id, person_id=person_id, **fields)
    except WriteRequestError as exc:
        return failure(exc.code, **({"problems": exc.problems} if exc.problems else {}))
    except KeyError:
        return failure("invalid-write-request")
    body = req["body"]
    return {"ok": True, "classification": "dry-run", "dispatch_attempted": False, "state": "unchanged", "method": req["method"],
            "path": req["path"], "body_keys": sorted(body[0] if isinstance(body, list) else body) if body else []}
