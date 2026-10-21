- Every digest item needs at least one source URL you actually opened or that a helper script
  returned. Never invent a release, date, version, quote or URL.
- Keep only items dated inside the window (inclusive). An item published earlier but discussed
  in the window is out unless something new shipped in the window.
- Leave out leaderboard and benchmark-number claims, funding and valuation news, hiring moves,
  and speculation, unless they ship something practitioners can use now.
- Merge duplicates: one item per release or event, with all its sources on that line.
- Patch and nightly releases matter only when their notes change what users can do; fold minor
  version bumps into one line per project.
- Write the digest with write_file to output/digest.md before you stop, even if few items
  qualify. List the sources you checked that had nothing in the window.
- When you need to decide whether a candidate belongs in the digest, call `load_skill` with
  `triage-release`.
