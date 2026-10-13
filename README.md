# CodeEscape

React/Vite participant and admin website with a Python/Flask API and MongoDB Atlas database.

The Vercel setup and required environment variables are in [DEPLOYMENT.md](DEPLOYMENT.md).

### Team default passwords

New teams with a blank password and spreadsheet imports use the leader’s full roll number (spaces removed, original case preserved). On participant login, an existing hash matching the old name-without-spaces + roll default accepts only the roll number and is upgraded automatically. Custom passwords and admin credentials are unchanged. Participant login includes a Show/Hide password button.

### Admin contestant practice

Use **Teams & access → Give test** to switch teams and members, practise Stage 1 chambers, or answer the selected team’s assigned Stage 2 match questions. Assigned chamber questions are used when available; otherwise choose a question of the matching kind. Answers and explanations are admin-only. Drafts and submitted practice answers stay local while the workspace is open.

**Apply practice results to real test** requires an explicit confirmation. The server recomputes correctness, validates assignments and timers, requires the corresponding event stage to be open, and records an audit entry. Existing submissions cannot be overwritten. Stage 1 conversion applies normal elimination/qualification; Stage 2 conversion requires a live match and contributes to normal scores, timing and advancement. Recorded practice solving time is used for newly created attempts and match scoring.

Participant usernames accept the leader’s full name without spaces (case-insensitive) or their email. If multiple leaders share the same normalized name, use email to identify the team. Default passwords use the exact stored leader ID (B2… or roll number such as 24ESKCA…), without whitespace and preserving case. Custom passwords remain unchanged.
