"""Declarative inventory of every Ancestry endpoint we have evidence of.

Each entry is a dict; the validator (scripts/validate_api.py) walks this list,
substitutes sample params, calls each endpoint, and records the response shape.
The output drives `ancestry-api-reference.md`.

Sample IDs below are placeholders, not real data:
  treeId = 222222222, personId = 200000001
  userId   = ?           (discovered at runtime from /facts page)
  recordId = 20000002 + collectionId = 62308 (a 1950 census record)
  categoryId = 34 (Birth, Marriage & Death) — exists for all subjects
  collectionId = 6224 (1930 U.S. Federal Census) — well-populated reference

Fields:
  group           — domain bucket
  name            — snake_case tool-style identifier (NOT a final tool name)
  method          — "GET" | "POST"
  path            — URL with {curly} placeholders for path params
  query           — dict of {param: {sample, required, note}}
  body            — dict (POST only)
  kind            — "json" | "html_research_data" | "html_preloaded_state"
                    | "html" | "binary" | "json_in_html_body"
  side_effect     — True means skip validation; document only
  feature_flagged — True means likely empty/disabled response
  source          — section of ancestry-spike-findings.md where first observed
  notes           — quirks worth surfacing in the reference
  needs           — list of derived params (e.g. ["imageId"]) populated in pass 2
"""

# Static sample identifiers for pass 1 substitution.
SAMPLE = {
    "treeId": "222222222",
    "personId": "200000001",
    "categoryId": "34",
    "subCategoryId": "bmd_birth",  # subcategory IDs are strings, not numeric
    "collectionId": "6224",  # 1930 U.S. Federal Census
    "recordId": "20000003",  # a 1930 census record
    "databaseId": "6224",
    "namespace": "1093",
    "mediaId": "00000000-0000-4000-8000-000000000003",
    "treeName": "Example-Family-Tree",
    "exportId": "6b3b76d3",  # placeholder; export endpoint returns latest
    "userId": "00000000-0000-0000-0000-000000000000",  # the signed-in account id; the CLI reads it from the facts page at runtime
}


ENDPOINTS = [

    # ---- Auth (dead paths; documented for completeness) ----
    {
        "group": "auth", "name": "pre_authenticate", "method": "POST",
        "path": "/account/signin/api/pre-authenticate",
        "side_effect": True, "kind": "json",
        "source": "§1",
        "notes": "Programmatic login is BLOCKED by Cloudflare + Ancestry fraud check (§1, §10, project_ancestry_auth_strategy memory). Documented only — must not call."
    },
    {
        "group": "auth", "name": "authenticate", "method": "POST",
        "path": "/account/signin/api/authenticate",
        "side_effect": True, "kind": "json",
        "source": "§1",
        "notes": "Same as above. Cookies must come from the user's existing browser session."
    },

    # ---- Trees ----
    {
        "group": "trees", "name": "list_owned_trees", "method": "GET",
        "path": "/api/treesui-list/trees",
        "query": {
            "rights": {"sample": "own", "required": True, "note": "own | shared"},
            "page": {"sample": "1", "required": False},
            "limit": {"sample": "10", "required": False},
        },
        "kind": "json", "source": "§12",
    },
    {
        "group": "trees", "name": "list_shared_trees", "method": "GET",
        "path": "/api/treesui-list/trees",
        "query": {"rights": {"sample": "shared"}, "page": {"sample": "1"}, "limit": {"sample": "10"}},
        "kind": "json", "source": "§12",
    },
    {
        "group": "trees", "name": "tree_info_uhome", "method": "GET",
        "path": "/api/uhome/secure/rest/user/tree-info",
        "query": {"tree_id": {"sample": SAMPLE["treeId"], "required": True}},
        "kind": "json", "source": "§10",
        "notes": "PRIMARY tree-info endpoint. Replaces the older /api/treesui-list/trees/{id}/info which now 404s."
    },
    {
        "group": "trees", "name": "tree_info_treeviewer", "method": "GET",
        "path": "/api/treeviewer/tree/{treeId}/info",
        "kind": "json", "source": "§12",
        "notes": "Alternate tree-info shape; tree viewer specific fields."
    },
    {
        "group": "trees", "name": "person_count", "method": "GET",
        "path": "/api/treesui-list/trees/{treeId}/personscount",
        "kind": "json", "source": "§3",
        "notes": "VALIDATION DRIFT (2026-05-02): bare GET now returns 400 'Missing params: name or tag or fs'. §3 documented as no-param. Param shape changed; needs a fresh probe to find the correct query."
    },

    # ---- Tree export ----
    {
        "group": "export", "name": "start_or_get_export", "method": "GET",
        "path": "/api/treesui-list/trees/{treeId}/export",
        "query": {"start": {"sample": "true", "required": False, "note": "?start=true triggers export OR returns cached one"}},
        "kind": "json", "source": "§2, §10",
        "notes": "Idempotent — returns existing export if one already exists. Cannot force fresh."
    },
    {
        "group": "export", "name": "poll_export_status", "method": "GET",
        "path": "/api/treesui-list/trees/{treeId}/export",
        "kind": "json", "source": "§2",
        "notes": "Same path, no query — returns status JSON. WAITING → RUNNING → FINISHED."
    },
    {
        "group": "export", "name": "download_gedcom", "method": "GET",
        "path": "/api/media/retrieval/v2/stream/namespaces/61515/media/{exportId}.ged",
        "query": {
            "Client": {"sample": "Ancestry.Trees", "required": True},
            "filename": {"sample": SAMPLE["treeName"], "required": False},
        },
        "kind": "binary", "source": "§2, §10",
        "notes": "Returns ZIP, not raw GEDCOM. ZIP contains one .ged file. Namespace 61515 is hardcoded.",
        "needs": ["exportId"],
    },

    # ---- People ----
    {
        "group": "people", "name": "root_person", "method": "GET",
        "path": "/api/treesui-list/trees/{treeId}/rootperson",
        "query": {"isGetFullPersonObject": {"sample": "true"}},
        "kind": "json", "source": "§3, §12",
    },
    {
        "group": "people", "name": "recently_viewed_person", "method": "GET",
        "path": "/api/treesui-list/trees/{treeId}/recentlyviewedperson",
        "query": {"isGetFullPersonObject": {"sample": "true"}},
        "kind": "json", "source": "§12",
    },
    {
        "group": "people", "name": "tree_persons_by_category", "method": "GET",
        "path": "/api/uhome/secure/rest/user/tree-persons",
        "query": {
            "tree_id": {"sample": SAMPLE["treeId"]},
            "category": {"sample": "LAST_UPDATED", "note": "LAST_UPDATED | RECENT | FAMILY"},
        },
        "kind": "json", "source": "§3, §12",
    },
    {
        "group": "people", "name": "make_discoveries", "method": "GET",
        "path": "/api/uhome/secure/rest/make-discoveries/v2",
        "query": {"tree_id": {"sample": SAMPLE["treeId"]}},
        "kind": "json", "source": "§3",
        "notes": "VALIDATION DRIFT (2026-05-02): GET 405 Method Not Allowed; POST 400 Bad Request. §3 documented as GET — endpoint contract has changed. Behavior unconfirmed under any method."
    },
    {
        "group": "people", "name": "person_facts_page", "method": "GET",
        "path": "/family-tree/person/tree/{treeId}/person/{personId}/facts",
        "kind": "html_research_data", "source": "§12",
        "notes": "HTML page; parse `window.researchData = {...};` for PersonFacts, PersonSources, UGCPersonSources, PersonFamily, PersonWebLinks, NewPersonHints, etc. Use brace-walk parser, not regex (DOTALL fails on 50KB JSON).",
    },
    {
        "group": "people", "name": "person_facts_glue_nodata", "method": "GET",
        "path": "/family-tree/person/facts/user/{userId}/tree/{treeId}/person/{personId}/factsgluenodata",
        "kind": "json", "source": "§12",
        "notes": "Returns UI shell only — does NOT include PersonFacts/PersonSources arrays. Use the full /facts page for source metadata.",
        "needs": ["userId"],
    },
    {
        "group": "people", "name": "story_recommendations", "method": "GET",
        "path": "/family-tree/person/facts/user/{userId}/tree/{treeId}/person/{personId}/storyrecommendations",
        "feature_flagged": True, "kind": "json", "source": "§13",
        "notes": "Returned empty list in test runs.",
        "needs": ["userId"],
    },

    # ---- Family ----
    {
        "group": "family", "name": "person_family_compact", "method": "GET",
        "path": "/api/uhome/secure/rest/user/person-family",
        "query": {
            "tree_id": {"sample": SAMPLE["treeId"]},
            "source_person_id": {"sample": SAMPLE["personId"]},
            "gens_up": {"sample": "2"},
        },
        "kind": "json", "source": "§12",
    },
    {
        "group": "family", "name": "new_family_view", "method": "GET",
        "path": "/api/treeviewer/tree/newfamilyview/{treeId}",
        "query": {
            "focusPersonId": {"sample": SAMPLE["personId"]},
            "isFocus": {"sample": "true"},
            "view": {"sample": "family"},
            "genup": {"sample": "4"},
            "gendown": {"sample": "2"},
        },
        "kind": "json", "source": "§12",
        "notes": "Returns Persons[] graph for tree-view UI. ~165 people at genup=4, gendown=2."
    },

    # ---- Person picker ----
    {
        "group": "people", "name": "person_picker_trees", "method": "GET",
        "path": "/api/person-picker/trees",
        "query": {"timestamp": {"sample": "1700000000000"}, "rights": {"sample": "all"}},
        "kind": "json", "source": "§15",
    },
    {
        "group": "people", "name": "person_picker_profile", "method": "GET",
        "path": "/api/person-picker/person/{treeId}/{personId}/",
        "kind": "json", "source": "§13, §15",
        "notes": "Compact form-fillable profile: GivenName, Surname, Gender, FamilyMembers[], LifeEvents[]."
    },
    {
        "group": "people", "name": "person_picker_suggest", "method": "GET",
        "path": "/api/person-picker/suggest/{treeId}",
        "query": {
            "partialFirstName": {"sample": "Zig"},
            "partialLastName": {"sample": "Mor"},
            "isHideVeiledRecords": {"sample": "true"},
        },
        "kind": "json", "source": "§13",
    },

    # ---- Hints ----
    {
        "group": "hints", "name": "hint_count", "method": "GET",
        "path": "/api/treeviewer/hints/tree/{treeId}",
        "query": {"pid": {"sample": SAMPLE["personId"]}},
        "kind": "json", "source": "§13",
        "notes": "Lightweight count only. For actual records use PersonHintsList."
    },
    {
        "group": "hints", "name": "person_hints_list", "method": "GET",
        "path": "/hintsui-personhints/api/PersonHintsList",
        "query": {
            "treeId": {"sample": SAMPLE["treeId"]},
            "personId": {"sample": SAMPLE["personId"]},
            "pagename": {"sample": "PersonHints"},
            "pubHistoryId": {"sample": "", "note": "blank for default page"},
        },
        "kind": "json_in_html_body", "source": "§13",
        "notes": "Returns JSON {html: {body: '...'}}. Parse the inner HTML to extract data-pubdata records (TreeId, PersonId, HintId, databaseId, recordId, imageId, review URLs)."
    },
    {
        "group": "hints", "name": "hint_source_info", "method": "GET",
        "path": "/hintsui-personhints/api/sourceInfo",
        "query": {"databaseId": {"sample": SAMPLE["databaseId"]}},
        "kind": "json", "source": "§13",
        "notes": "Returns {titleText, aboutText} — useful collection-context metadata."
    },

    # ---- Search (HTML pages) ----
    {
        "group": "search", "name": "advanced_form", "method": "GET",
        "path": "/search/",
        "query": {
            "searchMode": {"sample": "advanced"},
            "searchOrigin": {"sample": "navigation_header"},
        },
        "kind": "html", "source": "§15",
        "notes": "Generic advanced-search form. No __PRELOADED_STATE__; structured form data lives at window.ancestry.search."
    },
    {
        "group": "search", "name": "search_by_name", "method": "GET",
        "path": "/search/",
        "query": {
            "searchMode": {"sample": "advanced"},
            "searchOrigin": {"sample": "navigation_header"},
            "name": {"sample": "Jane+Q_Example"},
        },
        "kind": "html_preloaded_state", "source": "§15",
        "notes": "Adding name= triggers result rendering with __PRELOADED_STATE__. Result quality depends on number of supplied facets."
    },
    {
        "group": "search", "name": "search_person_in_tree", "method": "GET",
        "path": "/search/tree/{treeId}/person/{personId}/",
        "query": {
            "usePUBJs": {"sample": "true"},
            "currentPageIsStart": {"sample": ""},
            "useCurrentPageInfo": {"sample": ""},
        },
        "kind": "html_preloaded_state", "source": "§13",
        "notes": "Highest-quality auto-built search. Redirects to /search/?{expanded query}. Use page.next or allowedPages for pagination — `pg=N`, NOT `page=N` (the latter returns 502)."
    },
    {
        "group": "search", "name": "category_lane", "method": "GET",
        "path": "/search/categories/{categoryId}/",
        "query": {"name": {"sample": "Jane+Q_Example"}},
        "kind": "html_preloaded_state", "source": "§14",
        "notes": "Append the redirected person-scoped query string to scope the lane to the target person. Top-result name match is a one-call relevance signal."
    },
    {
        "group": "search", "name": "collection_lane", "method": "GET",
        "path": "/search/collections/{collectionId}/",
        "query": {"name": {"sample": "Jane+Q_Example"}},
        "kind": "html_preloaded_state", "source": "§14",
        "notes": "Collection-specific result columns differ from global cards. e.g. 1930 census exposes `Parent or Spouse Names`, `Home in 1930`."
    },
    {
        "group": "search", "name": "record_page", "method": "GET",
        "path": "/search/collections/{databaseId}/records/{recordId}",
        "kind": "html", "source": "§13",
        "notes": "Indexed-record HTML. Contains #recordServiceData (label/value table) and household tables. Triggers downstream calls to discoveryui-contentservice."
    },

    # ---- Search (JSON API) ----
    {
        "group": "search", "name": "search_results", "method": "GET",
        "path": "/api/search-results",
        "query": {
            "name": {"sample": "Jane+Q_Example", "required": True},
            "birth": {"sample": "1900-1-1_springfield-sangamon-illinois-usa_5000"},
            "death": {"sample": "1970-1-1_springfield-sangamon-illinois-usa_5000"},
            "defaultFacets": {"sample": "PRIMARY_YEAR.PRIMARY_NPLACE"},
            "location": {"sample": ""},
            "priority": {"sample": "usa"},
            "searchMode": {"sample": "advanced"},
            "searchState": {"sample": "Refinement"},
            "useImageFacets": {"sample": "true"},
        },
        "kind": "json", "source": "§14",
        "notes": "Raw JSON main search. POST without query returns 404; only GET with full query string works. Response: {recordJudgments, searchSuggestions, results: {hitCount, items, facetMetadata, page, queryQualityScore, requestBase64, encodedQueryTerms, queryId, treeItem}, guidance, facetFiltersIgnored}."
    },
    {
        "group": "search", "name": "hit_counts", "method": "GET",
        "path": "/api/search-results/hit-counts",
        "query": {
            "name": {"sample": "Jane+Q_Example"},
            "birth": {"sample": "1900-1-1_springfield-sangamon-illinois-usa_5000"},
            "category": {"sample": "34"},
            "categoryBucket": {"sample": "rstp"},
            "itemsPerPage": {"sample": "20"},
            "pageNumber": {"sample": "1"},
            "paged": {"sample": "true"},
            "priority": {"sample": "usa"},
            "searchMode": {"sample": "advanced"},
        },
        "kind": "json", "source": "§14",
        "notes": "Per-category hit counts. Strong primitive for selecting which lanes to dive into. results.items[0].Collections[] gives top collections per lane with Token, Label, Count."
    },
    {
        "group": "search", "name": "collection_results", "method": "GET",
        "path": "/api/search-results/collection-results",
        "query": {
            "name": {"sample": "Jane+Q_Example"},
            "birth": {"sample": "1900-1-1_springfield-sangamon-illinois-usa_5000"},
            "collections": {"sample": SAMPLE["collectionId"]},
            "pcat": {"sample": "35"},  # parent category
            "priority": {"sample": "usa"},
            "searchMode": {"sample": "advanced"},
            "defaultFacets": {"sample": "PRIMARY_YEAR.PRIMARY_NPLACE"},
            "useImageFacets": {"sample": "true"},
        },
        "kind": "json", "source": "§14",
        "notes": "Collection-scoped JSON results. columns[] reflects collection-specific field labels."
    },
    {
        "group": "search", "name": "record_hover_card", "method": "GET",
        "path": "/api/search-results/record/{collectionId}/{recordId}/",
        "query": {
            "client": {"sample": "searchui-resultsui"},
            "viewType": {"sample": "Hover"},
            "pageName": {"sample": "ancestry us : search : results"},
            "hasOremCorrections": {"sample": "UNKNOWN"},
        },
        "kind": "json", "source": "§14",
        "notes": "GET returns hover-card detail: collectionId, recordId, imageId(s), displayFields[], features.contentRights, debugData."
    },
    {
        "group": "search", "name": "record_match_explainer", "method": "POST",
        "path": "/api/search-results/record/{collectionId}/{recordId}/",
        "query": {
            "client": {"sample": "searchui-resultsui"},
            "viewType": {"sample": "Hover"},
            "pageName": {"sample": "ancestry us : search : results"},
            "hasOremCorrections": {"sample": "UNKNOWN"},
        },
        "body": {"base64Request": "<results.requestBase64 from a search response>"},
        "kind": "json", "source": "§14, §16",
        "notes": "Same path as hover-card GET. POST adds matchCounts.{exact, similar, different} and explanations[]. Validated as a clean per-record scorer (§16). 0/0/0 means already-attached to person.",
        "needs": ["base64Request"],
    },
    {
        "group": "search", "name": "cluster_contributors", "method": "GET",
        "path": "/api/search-results/cluster-contributors",
        "query": {
            "clusterId": {"sample": "PLACEHOLDER"},
            "pg": {"sample": "1"},
            "visibility": {"sample": "public"},
            "searchLinkId": {"sample": "PLACEHOLDER"},
        },
        "kind": "json", "source": "§14",
        "notes": "Public member-tree contributors for a results.treeItem cluster. results.items[] = tree-person entries with source/media counts.",
        "needs": ["clusterId", "searchLinkId"],
    },
    {
        "group": "search_meta", "name": "match_explainer_settings", "method": "GET",
        "path": "/api/search-results/results-match-explainer/settings",
        "kind": "json", "source": "§14",
        "notes": "Returns whether match-explainer feature is enabled for current account."
    },
    {
        "group": "search_meta", "name": "start_a_tree_message_settings", "method": "GET",
        "path": "/api/search-results/start-a-tree-message/settings",
        "kind": "json", "source": "§14",
    },

    # ---- Search WRITE endpoints (DO NOT CALL) ----
    {
        "group": "search_write", "name": "save_last_search", "method": "POST",
        "path": "/api/search-results/last-search",
        "side_effect": True, "kind": "json", "source": "§14",
        "notes": "Side-effect: persists last-search state. Skip."
    },
    {
        "group": "search_write", "name": "smart_filtering_set", "method": "POST",
        "path": "/api/search-results/smart-filtering",
        "side_effect": True, "kind": "json", "source": "§14",
    },
    {
        "group": "search_write", "name": "smart_filtering_visibility", "method": "POST",
        "path": "/api/search-results/smart-filtering/visibility",
        "side_effect": True, "kind": "json", "source": "§14",
    },
    {
        "group": "search_write", "name": "match_explainer_settings_set", "method": "POST",
        "path": "/api/search-results/results-match-explainer/settings",
        "side_effect": True, "kind": "json", "source": "§14",
    },

    # ---- Search metadata ----
    {
        "group": "search_meta", "name": "browse_collection", "method": "GET",
        "path": "/api/browse-collection/{collectionId}",
        "kind": "json", "source": "§14",
    },
    {
        "group": "search_meta", "name": "browse_collection_type", "method": "GET",
        "path": "/api/browse-collection/{collectionId}/type",
        "kind": "json", "source": "§14",
    },
    {
        "group": "search_meta", "name": "browse_collection_hierarchy", "method": "GET",
        "path": "/api/browse-collection/{collectionId}/hierarchy",
        "kind": "json", "source": "§14",
    },
    {
        "group": "search_meta", "name": "category_info_v2", "method": "GET",
        "path": "/api/collection-info/v2/categories/{categoryId}",
        "query": {"cultureId": {"sample": "en-US"}},
        "kind": "json", "source": "§14",
        "notes": "Localized category title, description, child categories."
    },
    {
        "group": "search_meta", "name": "search_form_metadata", "method": "GET",
        "path": "/search/getSearchFormMetaData/{collectionId}/",
        "kind": "json", "source": "§14",
        "notes": "Returns searchformMetadata, widgetScript, authorizedFeatures. Flattened form schema = legal query params for that collection."
    },
    {
        "group": "search_meta", "name": "category_metadata_legacy", "method": "GET",
        "path": "/api/getCategoryMetaData",
        "query": {"categoryName": {"sample": "34"}, "culture": {"sample": "en-US"}},
        "kind": "json", "source": "§14",
    },
    {
        "group": "search_meta", "name": "facet_fields_category", "method": "GET",
        "path": "/api/facet-fields/categories/{categoryId}",
        "kind": "json", "source": "§14",
        "notes": "Field names valid for this category. Use to validate query params before hitting search."
    },
    {
        "group": "search_meta", "name": "facet_fields_subcategory", "method": "GET",
        "path": "/api/facet-fields/categories/{subCategoryId}",
        "kind": "json", "source": "§14",
        "notes": "Same path; subcategory IDs add fields beyond parent category."
    },
    {
        "group": "search_meta", "name": "facet_fields_collection", "method": "GET",
        "path": "/api/facet-fields/collections/{collectionId}",
        "kind": "json", "source": "§14",
    },
    {
        "group": "search_meta", "name": "facet_fields_global", "method": "GET",
        "path": "/api/facet-fields/global",
        "kind": "json", "source": "§14",
    },

    # ---- Records / Discovery service ----
    {
        "group": "record", "name": "shoebox_record", "method": "GET",
        "path": "/discoveryui-contentservice/api/shoebox/collection/{databaseId}/record/{recordId}",
        "kind": "json", "source": "§13",
    },
    {
        "group": "record", "name": "record_connections", "method": "GET",
        "path": "/discoveryui-contentservice/api/trees/recordconnections/collection/{databaseId}/record/{recordId}",
        "kind": "json", "source": "§13",
        "notes": "Linked-tree rows for a record. Returned empty for our test record; non-empty for popular records."
    },
    {
        "group": "record", "name": "ai_record_narrative", "method": "GET",
        "path": "/discoveryui-contentservice/api/ai-record-narrative/{recordId}:{databaseId}",
        "query": {
            "treeId": {"sample": SAMPLE["treeId"]},
            "personId": {"sample": SAMPLE["personId"]},
            "filter": {"sample": ""},
        },
        "feature_flagged": True, "kind": "json", "source": "§13",
        "notes": "Returned feature-flag metadata only, no narrative. Watch for product roll-out."
    },
    {
        "group": "record", "name": "suggested_records_for", "method": "GET",
        "path": "/discoveryui-contentservice/api/suggested-records-for/{recordId}:{databaseId}",
        "query": {
            "treeId": {"sample": SAMPLE["treeId"], "required": False},
            "personId": {"sample": SAMPLE["personId"], "required": False},
            "filter": {"sample": ""},
        },
        "kind": "json", "source": "§13, §16",
        "notes": "PATH SEGMENT must use raw colon `recordId:databaseId` or url-encoded `%3A`. Returns Ancestry's record-linkage cluster for that seed (the records Ancestry has linked to the same person across collections). treeId/personId are informational; behavior is determined by the seed. Hop-2 traversal converges — no new records."
    },

    # ---- Image viewer ----
    {
        "group": "image", "name": "image_viewer_page", "method": "GET",
        "path": "/imageviewer/collections/{databaseId}/images/{imageId}",
        "query": {"pId": {"sample": SAMPLE["recordId"]}},
        "kind": "html", "source": "§12",
        "notes": "HTML viewer; triggers downstream API calls.",
        "needs": ["imageId"],
    },
    {
        "group": "image", "name": "image_collection_id", "method": "GET",
        "path": "/imageviewer/api/collection/id",
        "query": {
            "pId": {"sample": SAMPLE["recordId"]},
            "dbId": {"sample": SAMPLE["databaseId"]},
            "isInstitutionalUser": {"sample": "false"},
            "treespage": {"sample": "1"},
        },
        "kind": "json", "source": "§12",
    },
    {
        "group": "image", "name": "image_simple_records", "method": "GET",
        "path": "/imageviewer/api/record/simple-records",
        "query": {
            "dbId": {"sample": SAMPLE["databaseId"]},
            "imageId": {"sample": "PLACEHOLDER"},
        },
        "kind": "json", "source": "§13",
        "notes": "All indexed people on the image — useful for household reasoning.",
        "needs": ["imageId"],
    },
    {
        "group": "image", "name": "image_index_panel", "method": "GET",
        "path": "/imageviewer/api/record/index-panel-data",
        "query": {"dbId": {"sample": SAMPLE["databaseId"]}, "imageId": {"sample": "PLACEHOLDER"}},
        "kind": "json", "source": "§13",
        "needs": ["imageId"],
    },
    {
        "group": "image", "name": "image_gridlines", "method": "GET",
        "path": "/imageviewer/api/image/gridlines",
        "query": {
            "dbId": {"sample": SAMPLE["databaseId"]},
            "imageId": {"sample": "PLACEHOLDER"},
            "imageWidth": {"sample": "5000"},
            "imageHeight": {"sample": "4000"},
        },
        "kind": "json", "source": "§13",
        "needs": ["imageId"],
    },
    {
        "group": "image", "name": "image_thumbnails", "method": "GET",
        "path": "/imageviewer/api/media/thumbnails",
        "query": {
            "dbId": {"sample": SAMPLE["databaseId"]},
            "imageId": {"sample": "PLACEHOLDER"},
            "prevThumbnails": {"sample": "10"},
            "nextThumbnails": {"sample": "10"},
            "includeSeparators": {"sample": "true"},
        },
        "kind": "json", "source": "§13",
        "needs": ["imageId"],
    },
    {
        "group": "image", "name": "image_record_connections", "method": "GET",
        "path": "/imageviewer/api/tree/person/record-connections",
        "query": {"dbId": {"sample": SAMPLE["databaseId"]}, "pId": {"sample": SAMPLE["recordId"]}},
        "kind": "json", "source": "§13",
    },

    # ---- Media ----
    {
        "group": "media", "name": "media_image_full", "method": "GET",
        "path": "/api/media/retrieval/v2/image/namespaces/{namespace}/media/{mediaId}",
        "query": {"client": {"sample": "TreesUI"}},
        "kind": "binary", "source": "§4, §11",
        "notes": "Namespace 1093 = user-uploaded tree media; 60564 = profile portraits; 1265 = source-record scans. No maxSide/imagequality params returns full original."
    },
    {
        "group": "media", "name": "media_image_record", "method": "GET",
        "path": "/api/media/retrieval/v2/image/namespaces/{sourceCollectionId}/media/{sourceImageId}.jpg",
        "query": {
            "client": {"sample": "PersonUI"},
            "securityToken": {"sample": "PLACEHOLDER"},
            "maxHeight": {"sample": "250"},
        },
        "kind": "binary", "source": "§12",
        "notes": "Source-record images. The {sourceCollectionId}/{sourceImageId}/securityToken triple MUST come from the same PersonSources[].RecordImageUrl entry — they are bound together by the token. Remove maxHeight for full-size; add download=true&scale=1&imagequality=HighQuality for download bytes.",
        "needs": ["securityToken", "sourceImageId", "sourceCollectionId"],
    },
    {
        "group": "media", "name": "media_thumbnail", "method": "GET",
        "path": "/api/media/retrieval/v2/thumbnail/namespaces/{namespace}/media/{mediaId}",
        "kind": "binary", "source": "§4",
    },
    {
        "group": "media", "name": "person_gallery", "method": "GET",
        "path": "/api/media/viewer/v1/trees/{treeId}/people/{personId}",
        "query": {
            "client": {"sample": "ugcgallery-fe"},
            "excludeInlineStories": {"sample": "1"},
            "collectionId": {"sample": "1030"},
            "page": {"sample": "1"},
            "rows": {"sample": "28"},
            "sort": {"sample": "-created"},
            "withTranscription": {"sample": "true"},
            "filter": {"sample": "Photo,Story,Audio,Video,Container", "note": "Multi-value: pass repeated &filter= per type"},
        },
        "kind": "json", "source": "§12",
        "notes": "Person-scoped gallery. objects[].url points to /api/media/retrieval/v2/image/namespaces/1093/media/{id}.jpg."
    },

    # ---- Tree writes (FR-001) — captured 2026-05-02 from ancestry-tree-edit.har ----
    {
        "group": "writes", "name": "fact_save", "method": "POST",
        "path": "/family-tree/person/factedit/user/{userId}/tree/{treeId}/person/{personId}/assertion/{assertionId}/save",
        "body": {
            "assertionId": "0 for ADD; existing assertion_id for EDIT",
            "eventType": "customevent | Birth | Death | Marriage | ...",
            "name": {"givenName": "", "surname": "", "suffix": ""},
            "gender": "Male | Female | Unknown",
            "date": "DD MMM YYYY or empty",
            "location": "free-form place string",
            "description": "free-form",
            "preferred": "null | true | false",
            "showMap": "bool",
            "showOnLifeStory": "bool",
            "title": "alternate-name title",
            "customEventTitle": "fact label (custom events only)",
        },
        "kind": "json", "source": "FR-001 ancestry-tree-edit.har",
        "side_effect": True,
        "notes": (
            "Add (assertionId='0') and edit (assertionId=<existing>) share one endpoint. "
            "Auth: cookies only, no CSRF. Response: {AttributeIds: ['<new_id>'], status: bool}."
        ),
    },
    {
        "group": "writes", "name": "fact_delete", "method": "GET",
        "path": "/family-tree/person/factedit/user/{userId}/tree/{treeId}/person/{personId}/assertion/{assertionId}/delete",
        "kind": "json", "source": "FR-001 ancestry-tree-edit.har",
        "side_effect": True,
        "notes": (
            "Delete a fact assertion. METHOD IS GET (writes-via-GET pattern in Ancestry's "
            "older endpoints). No body, no query params. Response: {status: bool}."
        ),
    },
    {
        "group": "merge", "name": "merge_status", "method": "GET",
        "path": "/api/hintsui-api/trees/{treeId}/merge/job/status",
        "query": {"location": {"sample": "http://treeservices.ancestryl1/..."}, "evalV": {"sample": "1.0.0-455994a"}},
        "kind": "json", "source": "genealogy c31 devtools HAR 2026-10-06",
        "side_effect": False,
        "notes": "Poll a merge job started by merge/job/upload. Response {pending,success,location}; the UI polls ~3x.",
    },
    {
        "group": "media_upload", "name": "media_stoken", "method": "GET",
        "path": "/api/media/upload/mapi/tree/{treeId}/media/{mediaId}/stoken",
        "kind": "json", "source": "genealogy c50 devtools HAR 2026-10-06", "side_effect": False,
        "notes": "Per-upload security token for the stream PUT. mediaId is a client-generated UUID.",
    },
    {
        "group": "writes", "name": "media_stream_put", "method": "PUT",
        "path": "/api/media/upload/v2/stream/namespaces/{namespace}/media/{mediaId}",
        "query": {"client": {"sample": "trees-uploadclient"}, "securityToken": {"sample": "from media_stoken"}, "format": {"sample": "json"}},
        "kind": "binary", "source": "genealogy c50 devtools HAR 2026-10-06", "side_effect": True,
        "notes": "Raw image bytes, Content-Type = the image mime type. namespace 1093 in the capture.",
    },
    {
        "group": "writes", "name": "media_attach_person", "method": "POST",
        "path": "/api/media/upload/mapi/tree/{treeId}/person/{personId}/media",
        "body": [{"id": "<mediaId>", "name": "title", "mimeType": "image/png", "attachAsPrimary": "false",
                  "additionalFileDetails": {"fileExtension": "png", "fileHeight": 1, "fileHWidth": 1, "fileSize": 1,
                                            "fileSubtype": "image", "fileType": "image"}}],
        "kind": "json", "source": "genealogy c50 devtools HAR 2026-10-06", "side_effect": True,
        "notes": "Attach an uploaded media item to a person. Response {attaches:[{id,treeMediaId,...}],attachPoint}.",
    },
    {
        "group": "writes", "name": "media_delete", "method": "DELETE",
        "path": "/api/media/viewer/api/trees/{treeId}/media/{mediaId}",
        "kind": "json", "source": "genealogy c51 devtools HAR 2026-10-06", "side_effect": True,
        "notes": "Permanently delete a media item from the tree (UI: Delete media, then confirm). Empty body, 200.",
    },
    {
        "group": "people", "name": "person_notes_get", "method": "GET",
        "path": "/family-tree/person/workspace/user/{userId}/tree/{treeId}/person/{personId}/getpersonnotes",
        "kind": "json", "source": "page JS route string; genealogy c63 2026-10-06", "side_effect": False,
        "notes": "Read the shared person Note (and stickies). Route string from the workspace bundle.",
    },
    {
        "group": "writes", "name": "person_notes_save", "method": "POST",
        "path": "/family-tree/person/workspace/user/{userId}/tree/{treeId}/person/{personId}/savePersonNotes",
        "body": {"note": "free text"},
        "kind": "json", "source": "genealogy c63 devtools HAR 2026-10-06", "side_effect": True,
        "notes": "Save the shared person Note (replaces the whole note; empty string clears). Response {id, txt:'<line>..</line>', a, ...}.",
    },
    {
        "group": "writes", "name": "tag_add", "method": "POST",
        "path": "/family-tree/person/workspace/user/{userId}/tree/{treeId}/person/{personId}/addtags",
        "body": {"tagnames": ["33"]},
        "kind": "json", "source": "genealogy c60 devtools HAR 2026-10-06", "side_effect": True,
        "notes": "Add MyTreeTags to a person. tagnames are numeric tag ids (33 = To Do). Response {statusCode, addedTags[]}.",
    },
    {
        "group": "writes", "name": "tag_remove", "method": "POST",
        "path": "/family-tree/person/workspace/user/{userId}/tree/{treeId}/person/{personId}/removetags",
        "body": {"tagnames": ["33"]},
        "kind": "json", "source": "genealogy c61 devtools HAR 2026-10-06", "side_effect": True,
        "notes": "Remove MyTreeTags from a person. Response {statusCode, removedTags[]}.",
    },
    {
        "group": "writes", "name": "hint_set_state", "method": "PATCH",
        "path": "/api/hintsui-api/trees/{treeId}/persons/{personId}/hints/{hintId}",
        "body": {"state": "deferred | rejected | pending"},
        "kind": "json", "source": "genealogy c40/c41 devtools HAR 2026-10-06",
        "side_effect": True,
        "notes": "Review drawer Maybe -> {state:'deferred'}, No -> {state:'rejected'}. Content-Type text/plain;charset=UTF-8. Response {}.",
    },
    {
        "group": "writes", "name": "hint_ignore", "method": "POST",
        "path": "/hintsui-personhints/api/IgnoreHint",
        "query": {"treeId": {"sample": "1"}, "personId": {"sample": "2"}, "hintId": {"sample": "3"},
                  "suppressConfirmation": {"sample": "true"}, "bePage": {"sample": "www.ancestry.com"}},
        "kind": "json", "source": "genealogy c20 devtools HAR 2026-10-06",
        "side_effect": True,
        "notes": "Ignore a person hint. All parameters in the query string, empty body, response {}.",
    },
    {
        "group": "writes", "name": "hint_defer", "method": "POST",
        "path": "/hintsui-personhints/api/DeferHint",
        "query": {"treeId": {"sample": "1"}, "personId": {"sample": "2"}, "hintId": {"sample": "3"},
                  "suppressConfirmation": {"sample": "true"}, "bePage": {"sample": "www.ancestry.com"}},
        "kind": "json", "source": "genealogy c21 devtools HAR 2026-10-06",
        "side_effect": True,
        "notes": "UI 'Don't ignore': moves an ignored hint back to Undecided. Same query shape as IgnoreHint, empty body.",
    },
    {
        "group": "writes", "name": "source_create", "method": "POST",
        "path": "/family-tree/person/sourceedit/user/{userId}/tree/{treeId}/source",
        "body": {"title": "custom source title"},
        "kind": "json", "source": "genealogy c02 devtools HAR 2026-10-06",
        "side_effect": True,
        "notes": "Create a custom source. Response {gid:{v:'<sourceId>:n:n'},title,cd,md}; sourceId is the first gid segment.",
    },
    {
        "group": "people", "name": "source_get", "method": "GET",
        "path": "/family-tree/person/sourceedit/user/{userId}/tree/{treeId}/source/{sourceId}",
        "kind": "json", "source": "probe 2026-10-06",
        "notes": "Probe: read one custom source (title). Unverified until confirmed on a sandbox source.",
    },
    {
        "group": "writes", "name": "source_delete", "method": "DELETE",
        "path": "/family-tree/person/sourceedit/user/{userId}/tree/{treeId}/source/{sourceId}",
        "kind": "json", "source": "genealogy source-delete devtools HAR 2026-10-06", "side_effect": True,
        "notes": "Delete a custom source and every citation of it (UI: Edit source, Delete this source, confirm). Empty body, 200, {}.",
    },
    {
        "group": "writes", "name": "citation_create", "method": "POST",
        "path": "/family-tree/person/sourceedit/user/{userId}/tree/{treeId}/person/{personId}/citation",
        "body": {"sourceId": "123456789", "title": "citation details", "url": ""},
        "kind": "json", "source": "genealogy c02 devtools HAR 2026-10-06",
        "side_effect": True,
        "notes": "Add a citation of an existing source to a person. Response {gid:{v:'<citationId>:n:<sourceId>'},sgid,pgid,title,cd,md}.",
    },
    {
        "group": "writes", "name": "fact_attach_source", "method": "POST",
        "path": "/family-tree/person/factedit/user/{userId}/tree/{treeId}/person/{personId}/assertion/{assertionId}/attachSource",
        "body": {"sourceCitationId": "123456789012", "databaseId": "", "recordId": ""},
        "kind": "json", "source": "genealogy c03 devtools HAR 2026-10-06",
        "side_effect": True,
        "notes": "Attach an existing citation to a fact. databaseId/recordId are empty for custom sources.",
    },
    {
        "group": "writes", "name": "fact_detach_source", "method": "POST",
        "path": "/family-tree/person/factedit/user/{userId}/tree/{treeId}/person/{personId}/assertion/{assertionId}/detachSource",
        "body": {"sourceCitationId": "123456789012", "databaseId": "", "recordId": ""},
        "kind": "json", "source": "genealogy c04 devtools HAR 2026-10-06",
        "side_effect": True,
        "notes": "Detach a citation from a fact.",
    },
    {
        "group": "writes", "name": "weblink_add", "method": "POST",
        "path": "/family-tree/person/facts/user/{userId}/tree/{treeId}/person/{personId}/weblinkadd",
        "body": {
            "webLinkHref": "https://example.com/...",
            "webLinkTitle": "free-form name for the link",
        },
        "kind": "json", "source": "FR-001 ancestry-tree-edit.har",
        "side_effect": True,
        "notes": (
            "Add a UGC web-page source to a person. Auth: cookies only. "
            "Response: {webLinkId: '<uuid>', status: bool}."
        ),
    },
    {
        "group": "writes", "name": "weblink_remove", "method": "GET",
        "path": "/family-tree/person/facts/user/{userId}/tree/{treeId}/person/{personId}/weblinkremove",
        "query": {"webLinkId": {"sample": "PLACEHOLDER", "required": True,
                                "note": "UUID returned from weblink_add or scraped from /facts page"}},
        "kind": "json", "source": "FR-001 ancestry-tree-edit.har",
        "side_effect": True,
        "notes": "Remove a UGC web link by webLinkId. METHOD IS GET. No body.",
    },
    {
        "group": "writes", "name": "remove_citation", "method": "POST",
        "path": "/family-tree/person/factedit/user/{userId}/tree/{treeId}/person/{personId}/citation/{citationId}/removecitation",
        "kind": "json", "source": "JOR-001 cdp_capture_har 2026-07-27 (live Remove-source flow)",
        "side_effect": True,
        "notes": (
            "Remove a record-source citation (SourceType 0) from a person. No request body; "
            "cookies-only auth; headers Content-Type/Accept application/json + Referer=facts page. "
            "Flagged side_effect so raw_request cannot call it. Implemented as ancestry_tools.remove_citation."
        ),
    },
    {
        "group": "writes", "name": "add_relative", "method": "POST",
        "path": "/family-tree/person/addedit/user/{userId}/tree/{treeId}/person/{personId}/addperson",
        "body": {
            "type": "Mother|Father|Sister|Brother|Spouse|Daughter|Son",
            "person": {"personId": "{personId}", "treeId": "{treeId}", "userId": "{userId}"},
            "values": {"radioTab": "New person", "fname": "", "lname": "",
                       "genderRadio": "", "statusRadio": "Deceased", "bdate": "", "bplace": ""},
        },
        "kind": "json", "source": "captured 2026-08-08 UI add-sibling flow (WIF-00x)",
        "side_effect": True,
        "notes": "Create a new person and link them as a relative (structural; always approval-gated). Implemented as ancestry_tools.add_relative."
    },
    {
        "group": "writes", "name": "add_person_nph", "method": "POST",
        "path": "/api/hintsui-api/trees/{treeId}/merge/job/upload/nph",
        "body": {"hintId": "", "relation": "mother", "personGid": "{personId}:1030:{treeId}",
                 "sourcePersonGid": "", "parents": {}},
        "kind": "json", "source": "captured 2026-08-08 hint-add-parent flow (WIF-001)",
        "side_effect": True,
        "notes": "Hint-based add-relative (copy source person + link as mother/father/sibling); followed by merge/job/post/confirmation. Structural; approval-gated."
    },
    {
        "group": "writes", "name": "update_person", "method": "POST",
        "path": "/family-tree/person/addedit/user/{userId}/tree/{treeId}/person/{personId}/updatePerson",
        "body": {"person": {"personId": "", "treeId": "", "userId": "", "gender": ""},
                 "values": {"fname": "", "lname": "", "genderRadio": "", "bdate": "", "bplace": ""}},
        "kind": "json", "source": "captured 2026-08-08 person edit flow",
        "side_effect": True,
        "notes": "Update a person's core facts (name/dates/places)."
    },
    {
        "group": "writes", "name": "remove_person", "method": "POST",
        "path": "/family-tree/person/tree/{treeId}/person/{personId}/removePerson",
        "body": {"name": "confirm-name"},
        "kind": "json", "source": "captured 2026-08-08 UI remove-person flow",
        "side_effect": True,
        "notes": "DELETE a whole person (irreversible; structural; approval-gated). Implemented as ancestry_tools.remove_person. NOTE: removes whole persons only; no relationship-only remove endpoint captured yet."
    },
    {
        "group": "merge", "name": "merge_comparison", "method": "GET",
        "path": "/api/hintsui-api/trees/{treeId}/persons/{personId}/comparison/",
        "query": {
            "sourceGid": {"sample": "RECORD_ID:COLLECTION_ID", "required": True},
            "restricttorootnode": {"sample": "false"},
            "type": {"sample": "Record"},
            "evalV": {"sample": "1.0.0-483543a"},
        },
        "kind": "json", "source": "FR-001 ancestry-write.har",
        "side_effect": False,
        "notes": (
            "Bootstrap read for the record-merge flow: returns RecordNodes / TreeNodes / "
            "Citations / Sources / Repositories that map almost directly onto the upload "
            "body. Used by merge_record_to_person / merge_record_to_person_api. Cookies-only."
        ),
    },
    {
        "group": "merge", "name": "merge_upload", "method": "POST",
        "path": "/api/hintsui-api/trees/{treeId}/merge/job/upload",
        "body": {
            "evalV": "query param 1.0.0-483543a",
            "Nodes": "transformed from comparison response",
            "TreeRelationshipsMap": "relationship decisions",
            "Source": "record source being attached",
        },
        "kind": "json", "source": "FR-001 ancestry-write.har",
        "side_effect": True,
        "notes": (
            "Commits a merged record to a tree person. Content-Type text/plain;charset=UTF-8 "
            "when called directly. Reachable ONLY via the dedicated merge tools; flagged "
            "side_effect so raw_request cannot call it. Cookies-only; no CSRF."
        ),
    },
    {
        "group": "merge", "name": "merge_confirmation", "method": "POST",
        "path": "/api/hintsui-api/trees/{treeId}/merge/job/post/confirmation",
        "body": {
            "evalV": "query param 1.0.0-483543a",
            "hintsToAccept": "[] for free-form save",
        },
        "kind": "json", "source": "FR-001 ancestry-write.har",
        "side_effect": True,
        "notes": (
            "Required cleanup POST immediately after merge_upload. Flagged side_effect so "
            "raw_request cannot call it. Cookies-only; no CSRF."
        ),
    },

    # ---- Media exploration (story/place enrichment) ----
    {
        "group": "media_explore", "name": "explore_criteria", "method": "GET",
        "path": "/api/media-explore/criteria/trees/{treeId}",
        "query": {"personId": {"sample": SAMPLE["personId"]}},
        "kind": "json", "source": "§13",
        "notes": "Derived criteria from person facts (dates, places). Drives place/public-media exploration."
    },
    {
        "group": "media_explore", "name": "explore_media", "method": "POST",
        "path": "/api/media-explore/media",
        "query": {"limit": {"sample": "20"}},
        "body": {"criteria": "<from explore_criteria>"},
        "kind": "json", "source": "§13",
        "side_effect": False,
    },
]


def all_endpoints() -> list[dict]:
    return ENDPOINTS


def callable_endpoints() -> list[dict]:
    """Endpoints safe to validate (no side effects, no destructive POSTs)."""
    return [e for e in ENDPOINTS if not e.get("side_effect")]
