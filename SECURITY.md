# Security policy

## Supported version

Security fixes target the latest release on the `main` branch.

## Reporting

Do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting for the repository owner. Include the affected version, reproduction steps, impact and a minimal proof of concept. Do not include private image libraries or databases.

## Data and network behavior

Image indexing, search and inference run locally. Network access is used only when the user requests optional dependency/model downloads. The application does not include telemetry.

Downloaded AI models and Python packages are third-party code/data with separate licenses and supply-chain risk. Release maintainers must review and pin dependency changes, run CI, and publish SHA-256 checksums. Production installers should be Authenticode-signed.
