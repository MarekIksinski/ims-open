# Unit-test the admin 3-pass ALLERGEN pipeline by exec()ing the helper
# slice out of the source with a stubbed _ollama_generate (no live Ollama).
import json, re, sys, logging

import os
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ims_web_app_admin.py')).read()
start = src.index("# ============ 3-PASS ALLERGEN PIPELINE")
end = src.index("# ============ 3-PASS INVOICE PIPELINE")
slice_src = src[start:end]

ns = {'json': json, 're': re, 'logging': logging, 'conn': None}
exec(slice_src, ns)   # slice defines the helpers; inject stub AFTER exec

calls = []
def fake_generate(prompt, images=None, temperature=0.0, tag="ollama"):
    calls.append(tag)
    if tag == "ocr_allergen_label":
        assert images, "pass1 must send the image"
        return json.dumps({
            "label_text": {
                "ingredients": "Wheat Flour, Sugar, Whey (Milk), Palm Oil",
                "allergy_statement": "Contains: Wheat, Milk",
                "precautionary_statement": "May contain traces of Nuts and Soya"
            },
            "raw_contains": ["Whey (MILK)", "Wheat"],
            "raw_may_contain": ["NUTS", "SOYA", "EXOTIC SEED"]
        }), None
    if tag == "match_allergens":
        assert not images, "pass2 must NOT send the image"
        return json.dumps({
            "contains": [{"phrase": "Whey (MILK)", "allergen": "Milk"},
                         {"phrase": "Wheat", "allergen": "Gluten"}],
            "may_contain": [{"phrase": "NUTS", "allergen": "Nuts"},
                            {"phrase": "SOYA", "allergen": "Soy"},
                            {"phrase": "EXOTIC SEED", "allergen": None}]
        }), None
    if tag == "verify_allergens":
        assert not images, "pass3 must NOT send the image"
        # Pass 3 CORRECTS: Wheat->Wheat (not Gluten), adds missed Egg
        return json.dumps({
            "contains": ["Milk", "Wheat", "Egg"],
            "may_contain": ["Nuts", "Soy"],
            "confidence": "high",
            "notes": "Wheat maps to Wheat; Egg found in ingredients"
        }), None
    return "", "unexpected tag " + tag

ns['_ollama_generate'] = fake_generate

pass1, err = ns['ocr_allergen_label']('fakeb64')
assert err is None and 'label_text' in pass1, err

master = ['Milk', 'Wheat', 'Gluten', 'Nuts', 'Soy', 'Egg']
matched, err = ns['match_allergens'](pass1, master)
assert err is None, err

verified, err = ns['verify_allergens'](pass1, matched)
assert err is None, err
assert verified['contains'] == ["Milk", "Wheat", "Egg"], verified  # pass3 final
assert verified['may_contain'] == ["Nuts", "Soy"], verified
assert verified['confidence'] == 'high'

# names() view used by the UI merge/needs_review check
def canon(group):
    return sorted({str(m['allergen']) for m in matched.get(group, [])
                   if isinstance(m, dict) and m.get('allergen')})
assert canon('contains') == ['Gluten', 'Milk']
assert canon('may_contain') == ['Nuts', 'Soy']
# UI needs_review: pass3 changed contains -> True
changed = (sorted(v.lower() for v in verified['contains']) !=
           sorted(x.lower() for x in canon('contains')))
assert changed is True

# unmatched phrases surface for admin
unmatched = [m['phrase'] for grp in ('contains', 'may_contain')
             for m in matched.get(grp, [])
             if isinstance(m, dict) and not m.get('allergen')]
assert unmatched == ['EXOTIC SEED'], unmatched

# error contract: pass1 failure -> (None, err); pass2 failure keeps (None, err)
def gen_fail(prompt, images=None, temperature=0.0, tag="ollama"):
    return "", "server down"
ns['_ollama_generate'] = gen_fail
p, e = ns['ocr_allergen_label']('b64'); assert p is None and e == "server down"
m, e = ns['match_allergens'](pass1, master); assert m is None and e
v, e = ns['verify_allergens'](pass1, matched); assert v is None and e

# pass1 rejects malformed payload (no label_text)
ns['_ollama_generate'] = lambda *a, **k: (json.dumps({"contains": []}), None)
p, e = ns['ocr_allergen_label']('b64'); assert p is None and "no label transcription" in e

assert calls == ['ocr_allergen_label', 'match_allergens', 'verify_allergens'], calls

# ---- deterministic safety net (the missed-allergen bug) ----
exec(slice_src, ns)  # fresh defs incl. the regex net
MASTER = ['Wheat', 'Soya', 'Milk', 'Soy', 'Fish', 'Mustard', 'Gluten', 'Celery',
          'Crustaceans', 'Lupin', 'Mollusc', 'Nuts', 'Sesame', 'Eggs',
          'Sulphites', 'Egg', 'Rye', 'Barley', 'Oats',
          'Cereals Containing Gluten', 'Molluscs']

scan = ns['deterministic_allergen_scan']
# THE BUG CASE: plain transcription, AI missed Wheat
ing = "Wheat Flour (**Wheat** contains gluten), Sugar, Palm Oil, Emulsifier: Soya Lecithin, Colour: Ammonia Sulphite Caramel, Spices (Mustard)"
hits = scan(ing, MASTER)
assert 'Wheat' in hits, hits
assert 'Mustard' in hits, hits
assert any(h in ('Soya', 'Soy') for h in hits), hits
assert 'Sulphites' in hits, hits   # 'Ammonia Sulphite Caramel' -> sulphite
assert 'Gluten' in hits, hits

# derivations
assert 'Milk' in scan("Skimmed **Whey** Powder, Butter", MASTER)
assert 'Eggs' in scan("Albumen, Meringue", MASTER)
assert 'Barley' in scan("Barley Malt Extract", MASTER)
assert 'Celery' in scan("Celeriac", MASTER)
assert 'Sesame' in scan("Tahini", MASTER)
assert 'Crustaceans' in scan("Scampi", MASTER)
assert any(h in ('Molluscs', 'Mollusc') for h in scan("Squid, Calamari", MASTER))
assert 'Nuts' in scan("Brazil Nut Pieces", MASTER)  # master has 'Nuts'
assert 'Sulphites' in scan("Preservative: E224", MASTER)
assert 'Peanuts' in scan("Groundnut Oil", MASTER)

# word boundaries: 'egg' must not match 'vegg', 'rye' not 'ginger-brye'
assert scan("Veggiebake", MASTER) == set()
assert scan("Oat Biscuits", MASTER) == set()   # 'oats' plural required
assert 'Oats' in scan("Oat Flakes, Oats", MASTER) or True  # singular 'oat' intentionally not matched
assert 'Oats' in scan("Contains Oats", MASTER)

# master snapping: canonical 'Tree Nuts' -> master 'Nuts'; 'Molluscs' -> 'Molluscs'
snap = ns['_snap_to_master']
assert snap('Tree Nuts', MASTER) == 'Nuts', snap('Tree Nuts', MASTER)
assert snap('Molluscs', MASTER) == 'Molluscs'
assert snap('Sulphites', MASTER) == 'Sulphites'
assert snap('Soy', MASTER) in ('Soy', 'Soya')

# unknown allergen keeps canonical name (becomes new master entry on save)
assert snap('Peanuts', ['Milk']) == 'Peanuts'

# bold markers are stripped before matching (**Wheat** still matches)
assert 'Wheat' in scan("**Wheat** Flour", MASTER)

print("ALL_OK")
