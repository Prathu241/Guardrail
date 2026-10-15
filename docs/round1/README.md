# Round 1 submission: Perceptron Hackathon 2026 (IITB–Optiver AI Innovation Lab)

Everything below comes from the official problem-statement PDF. Each requirement is checked
against what is in this folder.

## What Round 1 actually is

**An online abstract**, judged by a panel on the *plan*, not on code. It is **not** a slide deck.

| Rule from the PDF | Our submission | OK |
|---|---|---|
| Choose exactly one of the 10 problem statements | Theme 6 (AI You Can Actually Trust), Problem 9 (Catching an Agent Before It Acts on a Bad Idea) | ✅ |
| Suggested length **600–900 words** (roughly 1–1.5 pages) | 879 words in the eight sections (headings included), about 1.4 A4 pages | ✅ |
| **Optional 1-page** architecture/approach diagram ("a strong plus") | Page 3 of the PDF; also `architecture.svg` | ✅ |
| Code, results and prototypes are **not required** | Not required; we mention the prototype only as evidence of feasibility | ✅ |
| Submitted on the official Techfest website, **by the Team Leader only** | You do this | ☐ |
| **One** submission per team (more can disqualify) | You do this, once | ☐ |
| Team of at most 3; students with a valid institute ID | Team SaptaX, ID TM-200031A7855: Pratham S (leader), Varsha R, Anjalee | ✅ |

## The eight sections the PDF requires (in this order)

| Section | What the PDF asks for | Where in our abstract |
|---|---|---|
| Problem & theme chosen | Which problem, and 2–3 sentences on why | ✅ |
| Understanding of the challenge | The core technical difficulty in your own words | ✅ four specific difficulties |
| Proposed approach | Concrete method; named techniques, models, datasets, tools; no "we will use AI/ML" | ✅ LangGraph, 30 allowlist checks, risk-routed model review (provider-agnostic), SQLite transaction, outbox |
| Minimum viable | Map the plan to each Minimum Viable bullet | ✅ both bullets mapped |
| Stretch ambitions | Which stretch goals, and why they are the next step | ✅ both stretch goals |
| Novelty / differentiation | Say where you deviate from the obvious baseline | ✅ five differences |
| Evaluation plan | Metrics, baselines, datasets/simulators | ✅ ablation over four configurations; each figure states its configuration, cases, and that it is simulated (see `docs/EVIDENCE.md`) |
| Feasibility and risks | Skills of the 3 members, why it is doable in time, what could go wrong | ✅ named roles, dated milestones, risks |

## How it is scored

| Criterion | Weight | What reviewers check |
|---|---|---|
| Problem understanding & relevance | 20% | the real technical crux; realistic scope |
| Technical soundness & feasibility | 25% | valid method; suitable tools; buildable by 3 people in time |
| Innovation / novelty | 20% | beyond the obvious baseline in the starter resources |
| Clarity & structure | 10% | precise and specific, not buzzwords; the reviewer can picture the system |
| Impact & rigor of evaluation | 15% | meaningful metrics and baselines that prove the claim |
| Team fit & readiness | 10% | skill mix supports the plan; week-by-week path to the MVP |

Each criterion is scored 1–5 (5 = precise, deep, feasible, original). Weighted score =
Σ (score × weight), normalized to rank teams. About 25–30 teams in total go to the finals.

## Dates

| Event | Date |
|---|---|
| Registration closes | **15 October** |
| Round 1 submission deadline | **31 October** |
| Shortlist announced | 10 November |
| Midway check (1-page progress report) | 1 December |
| Finals at IIT Bombay (working build required) | 15–18 December |

## Files in this folder

| File | What it is |
|---|---|
| `Guardrail_Round1_Abstract.pdf` | **The file to upload**: the abstract (pages 1–2) and the architecture diagram (page 3) |
| `Guardrail_Round1_Abstract.docx` | The same abstract and diagram as an editable Word file (`node docs/round1/build_docx.js`) |
| `ABSTRACT.md` | The same abstract as text, for a web form that wants pasted text |
| `architecture.svg`, `architecture.png` | The diagram on its own, if the form takes a separate image |
| `CLAUDE_PROMPT.md` | Prompts to regenerate or restyle the PDF and diagram with Claude |
| `deck/` | Source of the optional 13-slide presentation (not required for Round 1) |
| `../EVIDENCE.md` | The evidence register: every number in the abstract and deck, with its configuration, case count, simulated-or-live status and reproduce command |

## Before you submit

1. Check the roles in "Feasibility and risks" match who actually does what (Team fit is 10% of
   the score). If you add skills or an institute name, keep the eight sections under 900 words.
2. Regenerate both files after editing: `python docs/round1/build_pdf.py` and
   `node docs/round1/build_docx.js`.
3. If you keep the GitHub link in the deck, make the repository public first.
4. The Team Leader uploads the PDF once, before 31 October.
