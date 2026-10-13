# CodeEscape deployment

The React website and Python API deploy as one Vercel project. MongoDB Atlas stores the event data.

## Deploy from GitHub

1. Import the GitHub repository in Vercel, or push these project files to the repository you already connected.
2. Set the **Root Directory** to the folder containing `vercel.json` and `api/`. Do not select `frontend`.
3. Set **Application/Framework Preset** to **Vite**. If the import screen shows **Services**, change that dropdown to **Vite**. The root `vercel.json` sets the framework, install command, build command, output directory, API route, and SPA routing.
4. In **Settings → Environment Variables**, add these for **Production**:

   | Name | Value |
   |---|---|
   | `MONGODB_URI` | MongoDB Atlas connection URI |
   | `MONGODB_DB` | `codeescape` |
   | `ADMIN_PASSWORD_HASH` | Admin password hash |
   | `ENVIRONMENT` | `production` |

   Paste secret values directly, without wrapping quotes. Do not put `backend/.env` in GitHub. Keep `VITE_API_URL` unset; the website and API use the same Vercel domain.

5. Save the variables and deploy. Changes to environment variables require a redeployment.

The root `requirements.txt` lists Python packages directly, avoiding the nested requirements include that failed during deployment. The framework is explicitly configured as `vite` in `vercel.json`.

## Check the deployment

- Open `https://YOUR-PROJECT.vercel.app/api/health`. Expect `{"ok":true,"database":"mongodb"}`.
- Open `/admin` and sign in with the configured admin password.
- Open `/` and test participant login.

The Atlas database has already been populated with the event data. Do not run the MySQL migration or seed commands during deployment.
