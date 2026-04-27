# Python environments — the mental model

You keep reinstalling Python because the model in your head is "Python is a thing on my computer." That's not quite right. **A Python install is a folder.** You can have many of them, side by side, all working. The problem isn't installing — it's *knowing which one you're talking to*.

This doc fixes that. Read it once. You shouldn't need to reinstall Python again.

---

## The three layers (where packages can live)

Every Python install is the same shape: an `python` interpreter binary plus a `site-packages/` folder. Imports look in `site-packages`. So when you `pip install foo`, it lands in *some* `site-packages` — the question is always which one.

There are three reasonable layers, and one bad one:

| Layer | site-packages location | When to use |
|---|---|---|
| **System Python** | `/usr/lib/python3.x/site-packages/` | Never install here. Fedora's `dnf` owns it; mixing in `pip install` breaks updates. |
| **User Python** | `~/.local/lib/python3.x/site-packages/` | Almost never. Only for one-off scripts that need a couple of packages. |
| **Project venv** | `<project>/.venv/lib/python3.x/site-packages/` | **Default. This is where almost everything should go.** |
| **Tool venv** | `~/.local/share/uv/tools/<tool>/...` (uv) or `~/.local/pipx/venvs/<tool>/...` (pipx) | CLI tools you want everywhere (`httpie`, `glances`, `ruff`). Each tool gets its own private venv; only the binary appears on `$PATH`. |

The mistake — `sudo pip install foo` into system Python — is what makes you "reinstall Python" later when things break. Don't do it. Anything you install with `pip` should land in a venv.

---

## What a venv actually is

It's a directory. That's the whole magic. When you do `uv venv` (or the older `python -m venv .venv`), you get something like this:

```
.venv/
├── bin/
│   ├── python              # tiny shim → points back at a "real" Python somewhere
│   ├── python3             # same
│   └── (any installed CLI tools, e.g. marker_single)
├── lib/
│   └── python3.13/
│       └── site-packages/  # ← packages installed for THIS project only
└── pyvenv.cfg              # config: which "real" Python this venv is chained to
```

Running `.venv/bin/python -c "import marker"` works because Python looks in *this* venv's `site-packages` first. Running plain `python -c "import marker"` outside the venv fails — different `site-packages`, different world.

**To "uninstall" a project's Python world:** `rm -rf .venv`. That's it. You haven't touched anything global.

**To start fresh:** `rm -rf .venv && uv venv && uv pip install <packages>`. Twenty seconds.

---

## `pip` vs `uv` (and why this venv has no `pip`)

`pip` is the original installer. Comes with most Python installs. Slow, no dependency-resolution by default, doesn't manage venvs.

`uv` is a modern replacement (single Rust binary, ~10× faster, manages venvs *and* packages, lives at `~/.local/bin/uv`). It's what made the venv at `~/Library/tools/.venv/`. Notice it didn't install `pip` into the venv — uv doesn't need pip, so it skips the bloat. That's why `~/Library/tools/.venv/bin/pip` doesn't exist.

**Rule:** if a venv was made with `uv`, manage it with `uv`. Don't `pip install` into a uv-made venv. They both work, but you'll end up with confused state if you mix.

You can usually tell by looking at `pyvenv.cfg`:
```
$ cat .venv/pyvenv.cfg
home = /home/contino/.local/share/uv/python/...   ← made by uv
implementation = CPython
uv = 0.11.0                                        ← made by uv
```

---

## The two patterns

### Pattern 1: per-project venv (the default)

```bash
cd ~/some-project
uv venv                        # creates .venv/ in the current dir
uv pip install requests pandas # installs into .venv/, nothing global

# Two equivalent ways to run:
.venv/bin/python script.py     # explicit — preferred for cron, scripts, IDEs
source .venv/bin/activate      # alters $PATH for this shell only; deactivate when done
python script.py
```

The activated shell prepends `.venv/bin` to `$PATH`, so `python` resolves to `.venv/bin/python`. It also sets `$VIRTUAL_ENV` so prompts can show a marker. `deactivate` reverses it.

For scripts, cron jobs, and systemd units, **always use the explicit `.venv/bin/python` path** — never rely on activation, because cron has no shell to activate in.

### Pattern 2: tool venv (for CLI tools you want everywhere)

```bash
uv tool install httpie         # installs httpie into its own venv at
                               # ~/.local/share/uv/tools/httpie/
                               # and symlinks `http` into ~/.local/bin/
http https://example.com       # just works, anywhere

uv tool list                   # see what's installed
uv tool upgrade httpie         # update one
uv tool uninstall httpie       # nuke its venv
```

(`pipx` does exactly the same thing if you prefer it.)

---

## Diagnosing "which Python ran my code?"

When something feels off, three commands tell you everything:

```bash
which python                      # which `python` would I run right now?
python -c "import sys; print(sys.executable)"   # absolute path of THIS python
python -c "import foo; print(foo.__file__)"     # where did `foo` come from?
```

Read the path:
- `/usr/lib/...` → system Python (managed by Fedora; don't touch).
- `~/.local/lib/...` → user-site (legacy/global-to-you).
- `<anywhere>/.venv/lib/...` → a project venv (your good place).
- `~/.local/share/uv/tools/<tool>/...` → a tool venv from `uv tool install`.

If `foo.__file__` shows up in a path you didn't expect, that's the bug.

---

## The cardinal rules

1. **One venv per project.** Don't share venvs across projects; conflicting deps will bite you.
2. **Never `pip install` into system Python.** Either use a venv, or `uv tool install` for CLI tools.
3. **Pick one manager per venv.** uv-made venv → `uv pip install`. pip-made venv → `pip install`. Don't mix.
4. **Always invoke `.venv/bin/python` explicitly in scripts and cron.** Activation is a shell convenience, not a thing crontab understands.
5. **To reset, delete the venv directory.** Don't reinstall Python.

---

## Cheatsheet — daily commands

```bash
# Create / destroy
uv venv                         # new .venv in cwd
rm -rf .venv                    # nuke it

# Install / uninstall
uv pip install <pkg>            # add package to .venv
uv pip install -r req.txt       # from a requirements file
uv pip uninstall <pkg>          # remove
uv pip list                     # what's installed?
uv pip freeze > requirements.txt  # snapshot exact versions

# Run
.venv/bin/python script.py      # explicit (preferred)
source .venv/bin/activate       # mutates $PATH; `deactivate` undoes
python script.py                # only after activate

# CLI tools (install once, use everywhere)
uv tool install <tool>          # e.g. httpie, glances, ruff
uv tool list
uv tool upgrade <tool>
uv tool uninstall <tool>

# Diagnostics
which python
python -c "import sys; print(sys.executable)"
python -c "import <pkg>; print(<pkg>.__file__)"
cat .venv/pyvenv.cfg            # who made this venv?
```

---

## How this applies to the library tools

- `~/Library/tools/.venv/` was made by `uv`. It holds `marker_pdf`, `mobi`, `pandoc`, etc.
- `run.sh` calls `$SCRIPT_DIR/.venv/bin/python` explicitly — never plain `python3`. That's why it works under cron/systemd without activation.
- The systemd service `library-ingest.service` inherits this — it runs `run.sh`, which uses the venv.
- To add a new dependency: `uv pip install --python ~/Library/tools/.venv/bin/python <pkg>`.
  Or, equivalently: `cd ~/Library/tools && source .venv/bin/activate && uv pip install <pkg> && deactivate`.
- To reset the whole thing if it ever gets weird: `rm -rf ~/Library/tools/.venv && cd ~/Library/tools && uv venv && uv pip install marker-pdf mobi tqdm watchdog`. Done in under a minute.
