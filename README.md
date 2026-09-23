# adstxt-watch

Once a week this reads the public ads.txt file of about 65 large publisher websites and the public sellers.json file of the ad exchanges they name, then writes down what changed since the last run.

The point: when a small ad network or reseller quietly disappears from exchanges, or an ad-tech company loses publishers week after week, it shows up here before anyone announces anything.

Results appear in the `data` folder after the first run. `data/latest-diff.md` is the file to read.

Runs every Sunday evening by itself, or on demand: Actions → Weekly ads.txt crawl → Run workflow.

Edit `publishers.txt` to change which websites are read, one per line.

Only public files are read. Nothing about who is being watched, or why, is stored here.
