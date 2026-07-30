# Moving GiftPulse into its own repository

GiftPulse was built as a standalone project but is currently living in a
subdirectory of the Twins repository. That is a transport detail, not a design
choice: the GitHub App running the build session lacked permission to create a new
repository (`403 Resource not accessible by integration`), and this was the only
writable remote available.

Nothing in the project depends on its parent — no shared imports, no shared config,
no path assumptions. Moving it out is a copy.

## Steps

1. Create an empty repository on GitHub (call it `giftpulse`). Do not initialise it
   with a README, so the first push is clean.

2. Extract this directory with its history intact:

   ```bash
   git subtree split --prefix=giftpulse -b giftpulse-only
   ```

3. Push that branch to the new remote as `main`:

   ```bash
   git push git@github.com:<you>/giftpulse.git giftpulse-only:main
   ```

4. Clone the new repository and confirm it stands on its own:

   ```bash
   git clone git@github.com:<you>/giftpulse.git && cd giftpulse
   make install && make test     # 54 tests, no network required
   make demo                     # one indexer cycle against mock venues
   ```

5. Delete `giftpulse/` from the Twins branch, and delete this file from the new
   repository — it describes a move that has already happened.

If you would rather not preserve history, `cp -r giftpulse /path/to/new-repo` and
`git init` there works just as well; the history is one commit.
