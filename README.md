## README.md replacement section

Replace the existing `## Installation` section, from `## Installation` through the `---` immediately before `## Start with a small area`, with:

## Installation

### 1. Clone the repository

```powershell
git clone https://github.com/miraflor/openplaces-ph.git
cd openplaces-ph
```

### 2. Install the environment

The reference environment is defined in `environment.yml`. It is the full runtime and test environment, including the external Osmium command-line tool used for OpenStreetMap source acquisition.

```powershell
conda env create -f environment.yml
conda activate openplaces-ph
```

If you use your own environment, install the Python dependencies plus `osmium-tool`. A plain `python -m pip install -e .` installs the Python package and Python dependencies, but cannot install the external `osmium` executable.

### 3. Install OpenPlaces PH

For the current release, use an editable install from the repository:

```powershell
python -m pip install -e .
```

This creates the `openplaces` command.

### 4. Authenticate with Hugging Face

Foursquare OS Places is accessed through Hugging Face.

After accepting access to the dataset:

```powershell
hf auth login
```

Do not place access tokens in the repository.

### 5. Verify the installation

```powershell
osmium --version
openplaces --help
python -m pytest -q
```

---
