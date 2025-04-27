# Project Setup

This project uses:

- [Prettier](https://prettier.io/) for website-related code formatting
- [pre-commit](https://pre-commit.com/) for enforcing formatting before each commit.

---

## 1. Clone the Repository

```bash
git clone <repo-url>
cd <project-folder>
```

## 2. Install Node.js Dependencies

Make sure you have Node.js and npm installed.

`npm install`

This will install Prettier as a dev dependency.

## 3. (Optional) Set Up Python Virtual Environment for pre-commit

### Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate         # On Windows: .venv\Scripts\activate
```

### Install pre-commit

`pip install pre-commit`

## 4. Install pre-commit Hooks

This will install the hooks defined in .pre-commit-config.yaml (for example, to run Prettier):

`pre-commit install`

### 5. Usage

Every time you commit, staged files will be automatically formatted by Prettier.

To manually run Prettier on all files:

`npx prettier --write .`

To manually run pre-commit on all files:

`pre-commit run --all-files`
