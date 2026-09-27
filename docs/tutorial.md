# Duetkifu tutorial

Duetkifu keeps a research project as two files that you and an AI agent write together: the **record** of every move of the research (`kifu.json`), dead ends included, and the **report** that tells the finished story (`report.json`).

![The research loop: open a move, compute with recorded scripts, close it (dead ends too, with why), decide on the tree, tell it in the report](research-loop.svg)

This tutorial uses the demo project in [`examples/demo-project`](../examples/demo-project): four cycles of a formulation search, with synthetic data (not experimental results) and a small record of 10 moves.

1. [Install](#1-install) (once)
2. [Start](#2-start)
3. [Read the record](#3-read-the-record)
4. [Edit the record on the tree](#4-edit-the-record-on-the-tree)
5. [Let the agent write, and check what it wrote](#5-let-the-agent-write-and-check-what-it-wrote)
6. [Where each number comes from](#6-where-each-number-comes-from)
7. [Large folders: search instead of reading](#7-large-folders-search-instead-of-reading)
8. [The report: review it with the agent](#8-the-report-review-it-with-the-agent)

Then: [raw data, habits and languages](#9-raw-data-habits-and-languages) and [what to do when something goes wrong](#when-something-goes-wrong).

You need a desktop computer with Chrome or Edge, and Python 3.8 or later (no packages to install).

## 1. Install

### With Claude Code in a terminal, VS Code or JetBrains

```
/plugin marketplace add Ashur5457/duetkifu
/plugin install duetkifu@duetkifu
```

Start a new session and type `/`: **duetkifu** is in the list. Update with `/plugin marketplace update duetkifu`; remove with `/plugin uninstall duetkifu@duetkifu`.

### With the Claude desktop app, or without the plugin system

In a terminal, in the folder where you want to keep Duetkifu (you need [git](https://git-scm.com/)):

```bash
git clone https://github.com/Ashur5457/duetkifu.git && python duetkifu/duetkifu.py install-skill
```

This installs the `/duetkifu` skill into `~/.claude/skills/`, where the desktop app, the terminal and the editor extensions all find it. To update: `git pull` in the `duetkifu` folder, then `python duetkifu.py install-skill` again. No git? [Download the ZIP](https://github.com/Ashur5457/duetkifu/archive/refs/heads/main.zip) and run `python duetkifu.py install-skill` in the unzipped folder.

### With other AI agents (Copilot, Cursor, Codex, Gemini CLI and others)

Once per data folder:

```bash
python path/to/duetkifu.py init-agent "path/to/data-folder"
```

It adds a short marked section to `AGENTS.md` and `CLAUDE.md` in the folder that tells any agent where the rules are. Everything else in those files is kept. Duetkifu never calls an AI model itself.

## 2. Start

**From Claude Code.** Open Claude Code in the folder that holds your raw data and type `/duetkifu`. Tell Claude what you want: a report from the data, a record of the research so far (from your reports and slides), or both. Claude proposes them, writes them after you agree, checks them and opens the page, already connected to the folder.

**Without an agent.** `python path/to/duetkifu.py "path/to/data-folder"`, or open [`duetkifu.html`](../duetkifu.html) in Chrome or Edge and click **Open project folder**. To follow this tutorial, open `examples/demo-project` (work on a copy: the page saves into it).

**Where files go.** Duetkifu keeps everything it writes in a `duetkifu/` subfolder of your data folder. Raw data stays *inside* the folder you open but *outside* `duetkifu/`, and is only read. What is computed from it goes *inside* `duetkifu/`.

```
my-experiment/                the folder you open
  Data_R1/run001.xlsx         raw data: stays where it is, only read
  duetkifu/                   made by Duetkifu
    kifu.json                 the research record
    report.json               the report
    derived_data/cells.csv    tables computed from the raw data
    scripts/make_cells.py     the scripts that compute them
```

A folder you already review with Duetsheet has a `duetsheet/` subfolder instead: Duetkifu opens it as it is. (The demo uses a simpler layout, with both files at the top and the raw data in `data/`.)

The four buttons at the top switch between **Report view**, **Report edit**, **Kifu view** and **Kifu edit**. The **?** button explains the mode you are in.

## 3. Read the record

Click **Kifu view**. The research question is on the left; each move follows the move it was based on.

![The record of the demo as a tree, with the move "Cycle 3" open](kifu-tree.png)

- **The shape says how a move ended**: a filled dot holds, a crossed circle is a dead end, an orange diamond corrects an earlier move, a hexagon is an independent audit, an open circle is not resolved yet, a dashed square is planned. The legend above the tree lists them all.
- **Arrows**: a dotted arrow is a clue (the good run of cycle 1 pointed at the region cycle 3 found), a dashed orange arrow a correction, a grey dashed arrow support.
- **The small dot at the lower left of a move** is its data chain: filled green when its numbers are recomputed from the raw files and nothing changed, red when something changed, a ring when its sources are named.
- **Click a move** to read it on the right: why it was made, the result, what it means, the population its numbers refer to, its data chain, and the comments on it.
- **Highlight** shows only the main path (the moves the report tells), the dead ends, the corrections and open questions, the moves you started, the checks that do not match, or the moves with no data chain yet. **Timeline** puts the moves on their dates; **List** shows them as cards.

In the demo, two dead ends are worth reading: *High additive B* failed because the idea was wrong (all three runs scored low), and *Cycle 2* ended inconclusive, until the correction *R1* showed that half of that cycle was spent on the edges on purpose.

## 4. Edit the record on the tree

Click **Kifu edit**. The first time, a short card explains the tree; **How to edit on the tree** brings it back.

![Editing a dead end: the result buttons beside the move, and the form with what you decide and what the agent wrote](kifu-edit.png)

On the tree:

- **Set the result**: select a move; the small buttons beside it set ✓ holds, ✗ dead end, ? no conclusion, ○ active. A dead end or no conclusion asks for its cause and one sentence why.
- **Move it**: drag a move to place it. Drop it onto another move to make it follow that move.
- **Arrows and new moves**: drag the **+** under the selected move onto another move to draw an arrow (clue, corrects, supports), or onto empty space to add the next move there. Click an arrow to change or delete it.
- **+ Next move** above the tree adds a move after the selected one.

The form on the right has three parts:

1. **You decide**: the title, the result, the cause of a dead end (and whether it is confirmed), and a review mark such as *good move* or *doubtful*.
2. **Written part**: why this move, the result, what it means, the population. This is the part the agent can write for you (section 5).
3. **Advanced** (closed): kind, dates, the branch it belongs to, a milestone, the list of arrows, the data chain, the source report.

Every change is saved to `kifu.json` at once, and listed under the move with who made it.

## 5. Let the agent write, and check what it wrote

Writing down why each move was made and what came of it is the part nobody has time for. Let the agent do it, and check.

- **Ask for it**: in the written part of a move, click **Ask the agent to fill this in**. It sends a comment on the move and asks the agent. The agent writes the text from the move's sources and names the files it used.
- **Or comment**: under any move, write a comment and pick a tag: **Fill in this move**, **Recompute this**, **Needs a clearer reason**, **Add to the report**, **Reopen this**. **Send and ask the agent** sends it; the agent answers under the comment.
- **Check it**: what the agent wrote and you have not looked at yet is marked in orange, with the count in the written part's heading. Edit it, or click **Looked at it** (or **Looked at all of it**). Looking without changing is recorded too.
- **What stays yours**: the result of a move, a confirmed cause, marks and milestones. The agent suggests a cause as unconfirmed; you tick **Confirmed**.

While you work with the agent, it keeps the record by itself: it opens a move before each thing it tries and closes it with the result, and a dead end with the reason (see "Keep the record as you work" in [`AGENTS.md`](../AGENTS.md)).

## 6. Where each number comes from

Every move says where its numbers come from, at one of three levels:

- **Recomputed**: steps of the data chain recompute them from the raw files. Each number the move states can have a **check**: the stated number, the recomputed one, and whether they match (yes, close, no, or not reproducible). A mismatch is listed for you; nothing is rewritten to agree.
- **Sourced**: the report, table, figure or slides the numbers were taken from, each with a fingerprint. If a file changes or goes missing, the move shows it.
- **No data**: a sentence saying why, for example "only discussed in a meeting" or "planned, not run yet".

`python duetkifu.py check <folder>` checks both files and lists the moves whose files changed, the checks that do not match, the causes you have not confirmed yet, and the moves with no data chain. `python duetkifu.py kifu tree <folder>` prints the record as text.

## 7. Large folders: search instead of reading

A real research folder can hold thousands of files and reports of several megabytes. The agent does not read them whole:

- `python duetkifu.py find <folder> "words"` searches the text of every file (reports by section, Word by heading, slides by slide, spreadsheets by sheet and header, the record by move), in any language, and prints short excerpts with where they are. The index is kept outside the folder, in `~/.duetkifu/index/`, and updated only for files that changed.
- `python duetkifu.py kifu show <folder> <move>` prints one move with its data chain and the parts of its sources that concern it.
- `python duetkifu.py extract <folder> <report.html>` writes the figures and tables of an HTML report or a slide deck into `derived_data/report_figures/`, with their section and caption. A move that points at them shows them in place.

## 8. The report: review it with the agent

The report works as in [Duetsheet](https://github.com/Ashur5457/duetsheet): the agent writes, you review, one button asks the agent to revise, every change is kept.

![View mode of the report](view-mode.png)

**Review** (**Report edit**): change titles, text and captions directly; under **Quick adjustments and download**, change a chart's type, axes or scale. On a chart, hold the mouse button and draw around the points you mean, as in a paint program, or click one point; **Box on figure** draws a rectangle. The comment stores the data range and the ids of the points, which is what the agent reads. Under every block, write a comment with tags or an **Example request**.

![A free-hand region and a single data point marked on a chart, with the comments in the panel](edit-annotate.png)

**Ask the agent to revise**: if Claude Code started Duetkifu, it listens in the background at no cost. The button hands it your comments; the page shows its progress and reloads. It answers under each comment; answer back with **Reply**. Without a listening agent, the button gives you a prompt to paste.

![The agent's replies under each comment, and Figure 1 now on a log axis](agent-reply.png)

**Check what changed**: the **Changes** tab lists every change of the round, by you or the agent, as inline differences; **Show full history** shows every round, and each change can be reverted. The timeline has one row per block and one column per round.

![The change history and the timeline](change-log.png)

**Tie the report to the record**: a chapter of the report (a text block with a title) can name the move it tells. Those moves make the main path of the tree. The comment tag **Add to the report** on a move asks the agent to write such a chapter.

## 9. Raw data, habits and languages

- **Raw data**: the **Folder** tab lists the data files; **Import** turns one into a dataset with its path and fingerprint. Every script your agent runs is recorded as a step, so each chart shows its data chain (*Source: cycles1-3.csv ← combine_cycles.py ← 3 raw files*), green when every file is as recorded and red when something changed. Put new files into the folder and the steps that should use them turn red.

![The Folder tab after importing a data file](folder-tab.png)

- **Habits**: put figures you like in `habits/figures/` and texts you wrote in `habits/writing/`; **Learn my habits** and your agent suggest a figure style and writing rules, which you tick to keep.

![Style suggestions learned from the habits folder](style-panel.png)

- **Languages**: the interface follows your browser (English, Traditional Chinese, Simplified Chinese, Japanese, Korean, Spanish); change it at the top right or add your own ([how](translating.md)). The data never changes with the language.

![The same report with the Japanese interface](language-ja.png)

## When something goes wrong

- Errors and warnings stay listed under **⚠** at the top, with details and **Copy all**. With the launcher they are also printed for your agent and saved in `errors.log`.
- **The launcher was closed** while the page stayed open: the page says so once and stops saving. Start it again and reload the page.
- A file the agent is writing is never half-read: the page keeps the last good version until the file reads correctly.
- Saves that fail because another program holds the file for a moment (OneDrive, for example) are retried.
