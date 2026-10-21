# Manual browser checklist (~5 minutes)

Things the automated tests cannot check because they need a real signed-in browser.
Run after each deploy. Note the date, browser, and any error text (screenshot errors).

Start: close every Desktop Web tab, open https://desktop-web.robweb.dev/, hard reload
(Mac: Cmd+Shift+R).

| # | Do this | Expect | Result |
|---|---|---|---|
| 1 | Sign in through Cloudflare if asked | App opens, bottom-right Backend shows ready | |
| 2 | Switch to profile `hermes-admin` | Switches with no error box | |
| 3 | Switch to a second profile (e.g. `research`), then back | Both switch with no error | |
| 4 | Send: `Reply with exactly: ROB-OK` | Reply `ROB-OK` arrives | |
| 5 | Change theme (light/dark), then back | No crash or blank screen | |
| 6 | Attach a small file and send it | File shows in the message; agent can read it | |
| 7 | Open the built-in browser pane, load a public page | Page appears in the pane | |
| 8 | Leave the tab until Cloudflare sign-in expires, then switch profile | Page reloads to Cloudflare sign-in — NOT "requires a ws:// URL" | |

If anything fails: screenshot the error box including **Details**, note the time and profile.
