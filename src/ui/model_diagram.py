"""Self-contained SVG ERD from this run's declared relationships and columns."""

from html import escape


def model_diagram(target, schemas, *, fact_name=None, all_fields=False, dark=False):
    facts = [f for f in target.get("facts", []) if fact_name is None or f["name"] == fact_name]
    linked = {link["dimension_model"] for fact in facts for link in fact.get("dimension_links", [])}
    dimensions = [d for d in target.get("dimensions", []) if fact_name is None or d["name"] in linked]
    links = [(f["name"], link["dimension_model"], pair)
             for f in facts for link in f.get("dimension_links", []) for pair in link.get("key_pairs", [])]
    cards, positions = [], {}
    card_width, row_height = 340, 26
    background, foreground, border, muted = (
        ("#111827", "#E5E7EB", "#374151", "#9CA3AF") if dark else
        ("#FFFFFF", "#172033", "#CBD5E1", "#64748B")
    )
    def text(x, y, value, *, fill=foreground, size=12, bold=False, max_chars=38):
        value = str(value)
        label = value if len(value) <= max_chars else value[:max_chars-1] + "…"
        return (f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" '
                f'font-weight="{600 if bold else 400}"><title>{escape(value)}</title>{escape(label)}</text>')
    max_height = 0
    for role, models, x in (("Fact", facts, 30), ("Dimension", dimensions, 640)):
        y = 35
        for model in models:
            name = model["name"]
            schema = schemas.get(name, {})
            fields = list(schema) or [f["name"] for f in model.get("output_fields", [])]
            keys = set(model.get("grain_key_fields", [])) | set(model.get("natural_key_fields", []))
            if model.get("surrogate_key"):
                keys.add(model["surrogate_key"])
            connected = {p["fact_field"] for f, d, p in links if f == name} | {p["dimension_field"] for f, d, p in links if d == name}
            visible = fields if all_fields else [field for field in fields if field in keys | connected]
            height = 76 + max(1, len(visible)) * row_height
            accent = "#0F766E" if role == "Fact" else "#2563EB"
            positions[name] = {"x": x, "y": y + 25, "fields": {field: y + 65 + i * row_height for i, field in enumerate(visible)}}
            card = [f'<rect x="{x}" y="{y}" width="{card_width}" height="{height}" rx="8" fill="{background}" stroke="{border}"/>',
                    f'<path d="M{x+8},{y} H{x+card_width-8} Q{x+card_width},{y} {x+card_width},{y+8} V{y+44} H{x} V{y+8} Q{x},{y} {x+8},{y}" fill="{accent}"/>',
                    text(x+12, y+19, name, fill="#FFFFFF", bold=True, max_chars=43),
                    text(x+12, y+35, role.upper(), fill="#FFFFFF", size=10)]
            for i, field in enumerate(visible):
                field_y = y + 65 + i * row_height
                card.append(text(x+12, field_y+4, field, bold=field in keys, max_chars=30))
                card.append(text(x+260, field_y+4, schema.get(field, ""), fill=muted, size=10, max_chars=10))
                if i < len(visible)-1:
                    card.append(f'<line x1="{x+8}" y1="{field_y+11}" x2="{x+card_width-8}" y2="{field_y+11}" stroke="{border}" opacity=".35"/>')
            if not visible:
                card.append(text(x+12, y+69, "No declared key fields", fill=muted))
            card.append(text(x+12, y+height-10, f"{len(visible)} displayed / {len(fields)} fields", fill=muted, size=10))
            cards.extend(card)
            y += height + 35
        max_height = max(max_height, y)
    edges = []
    for fact, dimension, pair in links:
        if fact not in positions or dimension not in positions:
            continue
        left, right = positions[fact], positions[dimension]
        y1 = left["fields"].get(pair["fact_field"], left["y"])
        y2 = right["fields"].get(pair["dimension_field"], right["y"])
        x1, x2 = left["x"] + card_width, right["x"]
        label = escape(f'{fact}.{pair["fact_field"]} → {dimension}.{pair["dimension_field"]}')
        edges.append(f'<g class="relationship"><title>{label}</title><path d="M{x1},{y1} C{x1+110},{y1} {x2-110},{y2} {x2},{y2}" fill="none" stroke="{muted}" stroke-width="1.6"/><circle cx="{x1}" cy="{y1}" r="3" fill="{muted}"/><circle cx="{x2}" cy="{y2}" r="3" fill="{muted}"/></g>')
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="1010" height="{max(200,max_height)}" style="font-family:system-ui,sans-serif">{"".join(edges+cards)}</svg>'
    # This iframe owns its scroll/zoom controls and never touches the host page.
    return '''<!doctype html><html><head><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;color:FG;font:13px system-ui;background:transparent}
    .toolbar{display:flex;gap:8px;align-items:center;height:40px}button{border:1px solid BORDER;border-radius:6px;background:BG;color:FG;padding:4px 12px;cursor:pointer}
    #viewport{height:440px;overflow:auto;border:1px solid BORDER;border-radius:8px;background:BG}
    .relationship:hover path{stroke:#F59E0B;stroke-width:3}
    </style></head><body><div class="toolbar"><button onclick="zoom(-.15)">−</button><span id="scale">100%</span><button onclick="zoom(.15)">+</button><button onclick="reset()">Reset</button><span>Hover a field or connection for its full name</span></div>
    <div id="viewport"><div id="canvas">SVG</div></div><script>
    let factor=1;const canvas=document.getElementById('canvas');const svg=canvas.querySelector('svg');
    const width=Number(svg.getAttribute('width')),height=Number(svg.getAttribute('height'));
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
    function draw(){svg.setAttribute('width',width*factor);svg.setAttribute('height',height*factor);document.getElementById('scale').textContent=Math.round(factor*100)+'%'}
    function zoom(delta){factor=Math.max(.4,Math.min(2,factor+delta));draw()}function reset(){factor=1;draw()}
    </script></body></html>'''.replace('BORDER',border).replace('BG',background).replace('FG',foreground).replace('SVG',svg)
