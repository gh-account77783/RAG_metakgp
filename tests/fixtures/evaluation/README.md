# MetaKGP 50-case evaluation fixture

`metakgp_50.json` is the frozen P4 evaluation contract. It was curated from the
checked-in `Crawler/cleaned_wiki.jsonl` development snapshot rather than generated
at evaluation time.

The 50 cases are intentionally balanced:

- 30 direct single-document facts
- 10 linked-document facts with a verified source-to-target graph edge
- 5 comparisons or aggregations requiring two documents
- 5 unsupported or time-sensitive questions that require abstention

Reference answers describe what the frozen snapshot supports. They are not claims
about current IIT Kharagpur people, courses, schedules, or services. Every
answerable case records exact supporting text and the source URL. Automated tests
verify those excerpts and graph edges against the snapshot hash. The hash uses
UTF-8 bytes after normalizing CRLF and lone CR line endings to LF, so Git checkout
settings cannot change the dataset identity.

P4 must report retrieval and answer behavior separately. A cited answer passes
only when required facts are correct and its eligible citations support those
facts. Finding the expected page without answering correctly is a retrieval pass
and an answer failure. An answer without supporting citations also fails.

The production importer does not accept JSONL. The P4 evaluation harness should
materialize only the referenced pages as temporary Markdown inputs and translate
the recorded graph edges into explicit `[[filename.md]]` references. This is a
test-data preparation step; it must not add JSONL or crawler behavior to the
shipped importer.

Do not regenerate or edit the fixture after observing P4 results. A correction
requires a new dataset ID, a written reason, and results reported separately for
each dataset version.
