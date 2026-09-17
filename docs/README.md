# docs/

* `AutoTradeBot-architecture.pdf` - how the app works: the twelve books, the components, the classes (UML),
  the threads, a play's life (sequence), the state machines, the database (ER), the research loop, the API,
  the important functions and a glossary. Written for a computer scientist.
* `AutoTradeBot-architecture.html` - the same page as HTML (the diagrams are inline SVG).
* `build_architecture_doc.py` - regenerates the HTML. Render it to PDF with headless Edge:

```
python docs/build_architecture_doc.py
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf="docs\AutoTradeBot-architecture.pdf" "file:///<full path>/docs/AutoTradeBot-architecture.html"
```
