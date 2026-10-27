# IMAX watcher

A small Python script that runs on GitHub Actions every 5 minutes and sends Telegram alerts when BFI IMAX (Waterloo) tickets or extra screenings appear.

## Files

- `imax_watch.py`: the watcher. Standard library plus `requests`.
- `watch_urls.txt`: pages to watch, one per line.
- `.github/workflows/imax.yml`: the schedule. Do not change the cron without asking.
- `imax_state.json`: written by the workflow. Never edit by hand.

## When I send you a BFI link

1. Add it to `watch_urls.txt` on its own line, with a short `# comment` above it naming the film.
2. If I say it is a listing page (a list of films, not one film), start the line with `listing: `.
3. Commit with a plain message such as "Watch Dune: Part Three".
4. Tell me in one line what you added. Remind me to merge if you opened a pull request.

Film page links look like:
`https://whatson.bfi.org.uk/imax/Online/default.asp?BOparam::WScontent::loadArticle::permalink=<name>`

## Rules

- Never put the Telegram token or chat ID in any file. They live in GitHub Actions secrets.
- The script only reads public pages and sends alerts. Do not add anything that books or buys tickets.
- Keep requests light. Do not add guessing of page addresses.
- Use plain British English, short sentences, and no em-dashes in any message text the script sends.
- If you change `imax_watch.py`, test it against a local fake page first, and tell me what you could not test.
- Every change to any file gets a version bump. Update `VERSION` in `imax_watch.py` (patch for a new link or small fix, minor for new behaviour) and tell me the new number. Name any bundle you send `imax-watcher-v<version>.zip`.

## If alerts stop or look wrong

Ask me to paste the log from a manual run of the workflow with debug ticked. The log shows pages read, dates found and Buy buttons counted for each page.
