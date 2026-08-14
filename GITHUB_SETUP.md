# Put this project on GitHub

The repository intentionally excludes `data/`, local tokens, temporary files,
and editor settings through `.gitignore`.

From the unzipped project folder:

```powershell
git init
git add .
git commit -m "Add resumable Philippine POI triangulation pipeline"
git branch -M main
```

Create an empty GitHub repository in your account, then connect it:

```powershell
git remote add origin https://github.com/YOUR-USERNAME/ph-poi-triangulation.git
git push -u origin main
```

Do **not** use `git add -f data/`. The generated national source caches and
outputs can be many gigabytes and are reproducible from the code.

GitHub Actions runs only the lightweight unit tests. It does **not** attempt the
whole-Philippines data pipeline on GitHub-hosted CI.
