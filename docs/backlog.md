# Backlog

- **Collection recommender.** Given a person (birth and death years, places, relatives), suggest which collections to search
  and in what order. Start from `collections find` hit counts, then weight by place and date overlap with each collection's
  title and browse hierarchy. Needs a bigger catalogue than the five-per-category the API returns: seed it by running
  `collections find` over many keywords and places.
- Collection browse by place (`browse_collection_hierarchy` already works: state, then county, then city).
- See also the open gaps in [architecture.md](architecture.md).
