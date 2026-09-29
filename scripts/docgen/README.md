# docgen — .md → team .docx

The team reads `.docx` files in `deliverables/`. Agents read the `.md` sources. **Edit the `.md`, then
rebuild. Never hand-edit a generated `.docx`.**

```sh
(cd scripts/docgen && npm install)   # once; versions are exact-pinned
scripts/docgen/build.sh              # rebuilds the architecture .docx
```

- Mermaid blocks are rendered to PNG by mermaid-cli, using local Google Chrome. Set `CHROME=` to change the path.
- The build cache lives in `scripts/docgen/.cache/`: diagram PNGs keyed by content hash, the generated
  Word-edition pages and the Chrome config. It is disposable and gitignored. `deliverables/` holds only the `.docx`
  files.
- Wide diagrams get their own landscape page. A long left-to-right chain is redrawn top-down.
- The contents page is a static, clickable list, so it looks the same in Word, Google Docs, Pages and Quick Look.
