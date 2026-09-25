# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org): patch releases fix breakage caused by
changes on the marketplace's side, minor releases add features.

## [Unreleased]

### Added
- Search with sorting (relevance, orders, price, newest), a price window and
  continuous pages; the listing SKU is reported because the listed price can
  belong to another variant.
- Product card: price of the selected SKU, all variants, coupons with the
  amount they give at the requested quantity and the next threshold, Choice
  combo terms, lots, delivery methods with dates and cost to a Russian city,
  seller standing, characteristics (loaded from the page widget), buyer
  protection and returns.
- Reviews with pages, sorting and filters (stars, photos, from Russia,
  follow-ups), plus aspect tags; comparison of up to 10 items.
- Fixed: empty delivery quotes (wrong ship-from country and source id),
  session self-healing inside the server, slow city lookup, prices as
  strings.
