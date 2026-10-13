# CodeEscape

React/Vite participant and admin website with a Python/Flask API and MongoDB Atlas database.

The Vercel setup and required environment variables are in [DEPLOYMENT.md](DEPLOYMENT.md).

### Team default passwords

New teams with a blank password and spreadsheet imports use the leader’s full roll number (spaces removed, original case preserved). On participant login, an existing hash matching the old name-without-spaces + roll default accepts only the roll number and is upgraded automatically. Custom passwords and admin credentials are unchanged. Participant login includes a Show/Hide password button.
