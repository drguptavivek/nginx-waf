# CRS rules: 
copy the official rules/ from your pinned CRS release into modsec/rules/ (don’t edit them). Keep your changes in rules/local-exclusions.conf and custom.d/*.conf.\


# Rule blocks for each site

Keep RULE BLOCKS non-overlapping so audits and diffs are readable:
-   100000–109999: org-wide/local common (local-exclusions.conf, custom.d/)
-   110000–119999: admin site(s)
-   120000–129999: API site(s)
-   130000–: public webwistes, One block opf 10000 ber domain


