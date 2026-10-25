"""
Run once: python seed.py ../דוח\ עצי\ ייצור.xlsx
Outputs data/products.json for the app to seed from.
"""
import sys
import json
import math
import os
import pandas as pd

def classify_component(name: str):
    d = str(name).strip() if name else ''
    checks = [
        (lambda x: x.startswith('קרטון'), 'קרטון', 25),
        (lambda x: x.startswith('מדבקה'), 'מדבקה', 500),
        (lambda x: x.startswith('תווית') or x.startswith('חבק') or x.startswith('קופסא'), 'קרטוניות', 500),
        (lambda x: x.startswith('גלילים'), 'גלילים', 10),
        (lambda x: 'שקית' in x, 'שקית', 100),
        (lambda x: 'ספוג' in x, 'ספוג', 100),
        (lambda x: 'בד' in x, 'בד', None),
        (lambda x: x.startswith('סחבות'), 'מייקרופייבר', 50),
        (lambda x: x.startswith('מטלית') and 'ללא תווית' in x, 'מייקרופייבר', 80),
        (lambda x: 'ללא תווית' in x or x.startswith('מטלית'), 'מייקרופייבר', 100),
        (lambda x: 'כרית' in x, 'כרית', 100),
    ]
    for check, ctype, rounding in checks:
        try:
            if check(d):
                return ctype, rounding
        except Exception:
            pass
    return 'אחר', None


def get_customer(name: str) -> str:
    name = str(name).strip()
    if ' - ' in name:
        return name.split(' - ')[0].strip()
    return 'כללי'


def main(xlsx_path: str):
    df = pd.read_excel(xlsx_path, header=0)
    df.columns = ['פריט', 'תיאור פריט', 'יחידת מידה', 'כמות', 'מחסן', 'עומק',
                  'סוג עץ מוצר', 'רצף נתיבים', 'שלב של נתיב', 'תאור שלב']

    products = []
    components = {}
    product_components = []

    current_product = None
    seen_pc = set()

    for _, row in df.iterrows():
        depth = row['עומק']
        item_id = str(row['פריט']).strip()
        name = str(row['תיאור פריט']).strip() if str(row['תיאור פריט']) != 'nan' else ''
        qty = row['כמות'] if not (isinstance(row['כמות'], float) and math.isnan(row['כמות'])) else 1.0
        unit = str(row['יחידת מידה']).strip() if str(row['יחידת מידה']) not in ('nan', 'NaN') else ''

        if depth == 1:
            current_product = item_id
            customer = get_customer(name)
            products.append({'id': item_id, 'name': name, 'customer': customer})
        elif depth == 2 and current_product:
            ctype, rounding = classify_component(name)
            if item_id not in components:
                components[item_id] = {
                    'id': item_id,
                    'name': name,
                    'type': ctype,
                    'unit': unit,
                    'rounding': rounding,
                }
            key = (current_product, item_id)
            if key not in seen_pc:
                seen_pc.add(key)
                try:
                    qty_f = float(qty)
                except (ValueError, TypeError):
                    qty_f = 1.0
                product_components.append({
                    'product_id': current_product,
                    'component_id': item_id,
                    'quantity': qty_f,
                })

    data = {
        'products': products,
        'components': list(components.values()),
        'product_components': product_components,
    }

    out_path = os.path.join(os.path.dirname(__file__), 'data', 'products.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(products)} products, {len(components)} components, {len(product_components)} BOM rows → {out_path}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        default = os.path.join(os.path.dirname(__file__), '..', 'דוח עצי ייצור.xlsx')
        main(default)
    else:
        main(sys.argv[1])
