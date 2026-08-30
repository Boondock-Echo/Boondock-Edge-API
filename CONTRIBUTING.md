# Contributing to Boondock Software

**DRAFT — For legal and engineering review. Contributions should remain disabled until the contributor agreement and process described below are finalized.**

Thank you for your interest in improving Boondock software. This repository is publicly visible under a source-available license; it is not an open-source project as that term is defined by the Open Source Initiative.

## Before submitting a contribution

Do not submit a pull request until Boondock Technologies, LLC has approved and enabled its Contributor License Agreement (CLA) process.

To preserve Boondock Technologies' ability to maintain, distribute, and commercially license the software, every contributor must sign the applicable CLA before a contribution can be accepted. A pull request, issue, email, forum post, or other submission does not by itself grant Boondock Technologies permission to incorporate the submitted material.

**Repository owner action required before accepting contributions:**

1. Have counsel approve `INDIVIDUAL-CLA.md` and, if organizations may contribute, `CORPORATE-CLA.md`.
2. Configure a CLA-checking workflow for pull requests.
3. Publish a privacy notice explaining what contributor information is collected and retained.
4. Document who may approve contributions and verify CLA status.

## Contribution requirements

After the CLA process is active, contributions should:

- be the contributor's original work or be accompanied by documented permission compatible with this repository's license and commercial licensing model;
- include no code, firmware, documentation, media, model, dataset, or other material copied from an incompatible source;
- preserve applicable copyright, attribution, patent, and third-party license notices;
- identify all new dependencies and their exact licenses;
- avoid dependencies whose terms would require the repository or a commercial Boondock product to be licensed under conflicting terms;
- include appropriate tests and documentation;
- avoid secrets, credentials, personal data, customer data, radio recordings containing protected information, export-controlled material, or confidential information; and
- comply with `LICENSE` and `SAFETY.md`.

## Safety-related contributions

Do not submit features, examples, configurations, or documentation that represent the software as suitable for:

- PSAP, 911, emergency dispatch, or computer-aided dispatch functions;
- primary emergency alerting or notification;
- fireground, medical, rescue, evacuation, or incident-command decision-making;
- guaranteed radio monitoring, transcription, translation, or keyword detection; or
- any use that relies on the software for life safety or protection of property.

Security vulnerabilities and defects that could affect users should be reported privately through the security-reporting method designated in `SECURITY.md` rather than disclosed in a public issue.

## Third-party dependencies

Every proposed dependency must be reviewed before acceptance. The pull request should state:

1. dependency name and version;
2. upstream project URL;
3. license name and license-text location;
4. whether it is linked, compiled, bundled, modified, or merely used as a development tool;
5. whether it is included in distributed firmware, binaries, containers, installers, or documentation; and
6. any attribution, source-disclosure, reciprocal-license, patent, trademark, or redistribution obligations.

## Contribution process

Once contributions are enabled:

1. Open an issue describing the proposed change unless the change is a small correction.
2. Keep each pull request focused on one change.
3. Add or update tests and documentation.
4. Confirm the CLA check has passed.
5. Complete the dependency and safety disclosures in the pull-request template.
6. Respond to review comments and maintain a clear commit history.

Submission does not guarantee acceptance. Boondock Technologies, LLC may decline, modify, defer, or discontinue any proposed contribution.

## Which agreement applies?

- Every contributor signs `INDIVIDUAL-CLA.md`.
- If an employer or other organization owns or may own the contribution, an authorized representative of that organization also signs `CORPORATE-CLA.md` and lists the contributor in Schedule A.
- A contributor must use a personal email on the individual agreement and a work email in the corporate agreement's authorized-contributor schedule.
- A CLA administrator must verify acceptance before a pull request is merged.

## Licensing questions

Website: https://boondockecho.com  
Email: ** riki@boondocktechnologies.com **

Copyright © 2026 Boondock Technologies, LLC. All rights reserved.
