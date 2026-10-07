import sys, os, json, importlib.util, copy
os.environ["TELEGRAM_BOT_TOKEN"]="x"; os.environ["TELEGRAM_CHAT_ID"]="y"
"""Tests del motor (monitor.py), sin red ni Telegram: la red, el state y los envíos
se simulan. Se cargan dos copias independientes del mismo motor: una con config
"tipo Naruto/Pokémon" (filtro global) y otra "tipo One Piece" (sin filtro global,
desaparecidos, re-sync). Uso: python3 tests/test_motor.py"""
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE=os.path.dirname(os.path.abspath(__file__))
def load(name):
    spec=importlib.util.spec_from_file_location(f"monitor_{name}", os.path.join(ROOT,"monitor.py"))
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
ok=fail=0
def check(cond,msg):
    global ok,fail
    if cond: ok+=1
    else: fail+=1; print("FAIL:",msg)

class Harness:
    """run_once con red, state y Telegram simulados."""
    def __init__(s, m, config, fetch):
        s.m=m; s.state={}; s.sent=[]; s.config=config
        m.load_config=lambda: copy.deepcopy(s.config)
        m.load_state=lambda: s.state
        m.save_state=lambda st: setattr(s,"state",json.loads(json.dumps(st)))
        m.fetch_site=lambda site,cfg,timeout=None,attempts=2: fetch(site)
        s.edits=[]; s._mid=[100]
        def fake_send(tok,chat,msg,*a,**k):
            s.sent.append(msg)
            if not s.ok_send: return False
            s._mid[0]+=1; return s._mid[0]
        m.edit_telegram=lambda tok,chat,mid,text,markup:(s.edits.append((mid,text,markup)),True)[1]
        m.send_telegram=fake_send
        s.ok_send=True
    def run(s,**k):
        n=len(s.sent); s.m.run_once(**k); return s.sent[n:]

def P(pid,title,stock=True,link=None,woo=False):
    import hashlib
    uid=hashlib.md5(f"{'woo' if woo else 'shopify'}:{pid}".encode()).hexdigest()
    return {"uid":uid,"legacy_uid":"L"+uid,"title":title,"link":link or f"https://x/{pid}","price":"1€","in_stock":stock,"stock_text":"","backorder":False}

# ===================== MOTOR COMÚN =====================
m=load("common")
base={"telegram_bot_token":"x","telegram_chat_id":"y","user_agent":"UA","silent_first_run":True,
      "required_keywords":["naruto"],"exclude_keywords":["kayou"],"notify_only_in_stock":True,
      "notify_new_oos_priority":True,"high_value_keywords":["booster box","display"],
      "priority_exclude":["fundas"],"health_fail_threshold":3,"health_digest_cooldown_minutes":0,
      "sites":[{"name":"A","url":"https://a.com/products.json","type":"api","priority":"high"}]}
catalog={"A":[P(1,"Naruto booster box",stock=False)]}
fetch=lambda site:(site,list(catalog.get(site["name"],[])),None) if catalog.get(site["name"]) is not None else (site,None,"down")
h=Harness(m,base,fetch)
check(h.run()==[], "1ª pasada silenciosa")
check(h.state["__sig__"]["A"]==m.site_signature(base["sites"][0],base), "firma guardada")
check("__run__" in h.state and h.state["__run__"]["sites_ok"]==1, "__run__ guardado")
catalog["A"].append(P(2,"Naruto display NT-01",stock=False))
msgs=h.run(); check(len(msgs)==1 and "AGOTADO" in msgs[0] and "display" in msgs[0].lower(), "nuevo agotado prioritario avisa")
catalog["A"].append(P(3,"Naruto starter deck",stock=False))
check(h.run()==[], "nuevo agotado NO prioritario no avisa")
catalog["A"].append(P(4,"Naruto fundas display 12 unidades",stock=False))
check(h.run()==[], "priority_exclude: fundas 'display' agotadas no avisan como prioritarias")
catalog["A"].append(P(5,"Naruto fundas display 12 unidades verdes",stock=True))
msgs=h.run(); check(len(msgs)==1 and "• 🚨" not in msgs[0] and "• 🆕 NUEVO" in msgs[0], f"fundas en stock llegan sin 🚨: {msgs}")
# cambio de URL -> rebaseline: nuevos absorbidos, restock sí
base["sites"][0]["url"]="https://a.com/products.json?limit=250"
catalog["A"].append(P(6,"Naruto booster box EN",stock=True))
catalog["A"][0]=P(1,"Naruto booster box",stock=True)   # restock del 1
msgs=h.run(); check(len(msgs)==1 and "VUELVE" in msgs[0] and "EN" not in msgs[0].split("VUELVE")[1][:40], "rebaseline: absorbe el nuevo, avisa el restock")
check("booster box EN" not in "".join(msgs), "rebaseline: el nuevo no aparece")
catalog["A"].append(P(7,"Naruto booster box JP",stock=True))
msgs=h.run(); check(len(msgs)==1 and "JP" in msgs[0], "tras rebaseline, lo nuevo vuelve a avisar")
# cambio de filtro global -> rebaseline
base["exclude_keywords"]=["kayou","mythos"]
catalog["A"].append(P(8,"Naruto booster box KR",stock=True))
check(h.run()==[], "cambio de exclude global -> rebaseline silencioso")
# fallo de envío: ni state ni firma se actualizan
base["exclude_keywords"]=["kayou"]
catalog["A"].append(P(9,"Naruto booster box CN",stock=True))
catalog["A"][0]=P(1,"Naruto booster box",stock=False); h.run()   # absorbe CN (rebaseline), 1 pasa a agotado
catalog["A"][0]=P(1,"Naruto booster box",stock=True)
h.ok_send=False; msgs=h.run(); check(len(msgs)==1, "restock intentado")
h.ok_send=True; msgs=h.run(); check(len(msgs)==1 and "VUELVE" in msgs[0], "restock reintentado tras fallo de envío")

# --- dedup por dominio: dos Woo distintas con el mismo id ---
cfg2=copy.deepcopy(base); cfg2["sites"]=[{"name":"W1","url":"https://w1.es/wp-json?x","type":"api"},{"name":"W2","url":"https://w2.es/wp-json?x","type":"api"},{"name":"W1b","url":"https://www.w1.es/otra","type":"api"}]
cat2={"W1":[],"W2":[],"W1b":[]}
h2=Harness(m,cfg2,lambda s:(s,list(cat2[s["name"]]),None)); h2.run()
for k in cat2: cat2[k].append(P(77,f"Naruto booster box {k}",woo=True))
msgs=h2.run(); txt="".join(msgs)
check("W1" in txt and "W2" in txt and "booster box W1b" not in txt, f"dedup por dominio: W2 avisa, W1b (mismo dominio) no: {msgs}")

# --- poda de tiendas huérfanas, sin tocar las medium con --priority ---
cfg3=copy.deepcopy(base); cfg3["sites"]=[{"name":"H","url":"https://h/x","type":"api","priority":"high"},{"name":"M","url":"https://m/x","type":"api","priority":"medium"}]
h3=Harness(m,cfg3,lambda s:(s,[P(1,"naruto display")],None)); h3.run()
h3.state["Vieja"]={"u":{"in_stock":True}}; h3.state["__health__"]["Vieja"]={"fails":0}
h3.run(priority_filter="high")
check("Vieja" not in h3.state and "Vieja" not in h3.state["__health__"] and "M" in h3.state, "poda huérfanas y respeta medium")

# --- histéresis de salud ---
cfg4=copy.deepcopy(base); cfg4["health_recover_passes"]=3
up={"v":False}
h4=Harness(m,cfg4,lambda s:(s,[P(1,"naruto display")],None) if up["v"] else (s,None,"down"))
for _ in range(3): h4.run()
check(any("no responden" in x for x in h4.sent), "caída avisada al umbral")
n=len(h4.sent); up["v"]=True; h4.run(); up["v"]=False; h4.run(); up["v"]=True; h4.run(); h4.run()
check(not any("Recuperadas" in x for x in h4.sent[n:]), "intermitente: no se da por recuperada")
h4.run(); check(any("Recuperadas" in x for x in h4.sent[n:]), "recuperada tras 3 buenas seguidas")
check(sum("no responden" in x for x in h4.sent)==1, "la recaída intermitente no re-avisa caída")

# --- HTML: detección de stock y título cortado ---
html='''<div id="js-product-list">
<article class="product-miniature"><h3 class="product-title"><a href="https://lacuevaroja.com/pokemon/123-pokemon-tcg-etb-30-aniversario-es.html">Pokemon TCG: ETB 30...</a></h3>
<span class="price">60€</span><ul><li class="product-flag out_of_stock">Fuera de stock</li></ul></article>
<article class="product-miniature"><h3 class="product-title"><a href="https://lacuevaroja.com/pokemon/124-lata.html">Lata 30 aniversario</a></h3><span class="price">20€</span>
<button class="add-to-cart">Añadir al carrito</button></article>
<article class="product-miniature"><h3 class="product-title"><a href="https://lacuevaroja.com/pokemon/125-blister.html">Blister 30 aniversario</a></h3><span class="price">5€</span></article>
</div>'''
site={"url":"https://lacuevaroja.com/pokemon-tcg","selector":"#js-product-list .product-miniature","title_selector":".product-title","link_selector":"a","price_selector":".price"}
ps=m.extract_products_html(html,site)
check(ps[0]["in_stock"] is False and "etb 30 aniversario es" in ps[0]["title"], f"PrestaShop: out_of_stock hijo + slug sin id: {ps[0]}")
check(ps[1]["in_stock"] is True, "carrito activo = en stock")
check(ps[2]["in_stock"] is False, "sin carrito en listado con carritos = agotado")
check(m.complete_truncated_title("NARUTO..","https://distritozero.es/naruto-booster-box-8412345678901.html").endswith("(naruto booster box)"), "Distrito Zero: quita EAN")
check(m.complete_truncated_title("Algo normal","https://x/y")=="Algo normal", "título normal intacto")

# --- crash: un solo aviso por error, se limpia al recuperarse ---
import pathlib
m.CRASH_FLAG=pathlib.Path(HERE)/".crashflag_test"; m.CRASH_FLAG.unlink(missing_ok=True)
sent=[]; m.send_telegram=lambda t,c,msg,*a,**k:(sent.append(msg),True)[1]
real=m.run_once
def boom(**k): raise KeyError("sites")
m.run_once=boom
for _ in range(3):
    try: m.run_once_guarded()
    except KeyError: pass
check(len(sent)==1 and "fallando" in sent[0], f"crash avisado una vez: {len(sent)}")
m.run_once=lambda **k: None; m.run_once_guarded()
check(not m.CRASH_FLAG.exists(), "flag borrado al recuperarse")
m.run_once=boom
try: m.run_once_guarded()
except KeyError: pass
check(len(sent)==2, "tras recuperarse, un fallo nuevo vuelve a avisar")

# --- moneda ---
d={"products":[{"id":1,"title":"x","handle":"x","variants":[{"price":"10.00","available":True}]}]}
check(m.extract_products_api(d,"https://a.co.uk/x",currency="£")[0]["price"]=="10.00£","moneda Shopify")
w=[{"id":2,"name":"y","permalink":"p","prices":{"price":"1000","currency_symbol":"&euro;"},"is_in_stock":True}]
check(m.extract_products_api(w,"https://w")[0]["price"]=="10.00€","símbolo Woo desescapado")

# ===================== ONE PIECE =====================
o=load("op")
ocfg={"telegram_bot_token":"x","telegram_chat_id":"y","user_agent":"UA","notify_only_in_stock":True,
      "silent_first_run":True,"mark_disappeared_oos":True,"sound_for_promo":True,"bot_label":"ONE PIECE",
      "notify_new_oos_priority":True,"resync_threshold":20,"high_value_keywords":["case","box","caja"],
      "priority_exclude":["deck box","card case"],"promo_keywords":["promo"],"health_digest_cooldown_minutes":0,
      "sites":[{"name":"LCR","url":"https://lacuevaroja.com/op?resultsPerPage=200","type":"html","priority":"high"}]}
ocat=[P(1,"OPCG Booster Box OP-15")]
ho=Harness(o,ocfg,lambda s:(s,list(ocat),None)); ho.run()
ocat.append(P(2,"OPCG Case Booster Box PEB01",stock=False))
msgs=ho.run(); check(len(msgs)==1 and "AGOTADO" in msgs[0], "OP: case nuevo agotado avisa")
ocat.append(P(3,"OPCG Deck Box negro",stock=False))
check(ho.run()==[], "OP: deck box agotado no avisa (priority_exclude)")
ocfg["sites"][0]["url"]="https://lacuevaroja.com/op?resultsPerPage=300"
ocat.append(P(4,"OPCG Booster Box OP-16"))
check(ho.run()==[], "OP: cambio de URL -> rebaseline silencioso")
ocat.append(P(5,"OPCG Booster Box OP-17"))
check(len(ho.run())==1, "OP: después avisa lo nuevo")
# re-sync conserva el 🚨
ocat.extend(P(100+i,f"OPCG Starter Deck ST-{i}") for i in range(21)); ocat.append(P(200,"OP-18 Case (12 Booster Box)"))
msgs=ho.run(); check(any("OP-18 Case" in x for x in msgs) and not any("ST-3 " in x for x in msgs), "OP: re-sync avisa el case y absorbe el resto")
# desaparecido -> agotado -> reaparece = restock
ocfg["sites"]=[{"name":"ISK","url":"https://isekai/x","type":"html"}]
icat=[P(i,f"OP Booster Box {i}") for i in range(1,8)]
hi=Harness(o,ocfg,lambda s:(s,list(icat),None)); hi.run()
gone=icat.pop(2); check(hi.run()==[], "desaparece: sin aviso")
check(hi.state["ISK"][gone["uid"]]=={"in_stock":False,"gone":True}, "desaparecido marcado agotado")
icat.append(gone); msgs=hi.run(); check(len(msgs)==1 and "VUELVE" in msgs[0], "reaparece = RESTOCK")
# tope lleno: no marca desaparecidos
ocfg["sites"]=[{"name":"CAP","url":"https://c/products.json?limit=5","type":"api"}]
ccat=[P(i,f"OP Booster Box {i}") for i in range(10,15)]
hc=Harness(o,ocfg,lambda s:(s,list(ccat),None)); hc.run(); ccat[0]=P(99,"OP Booster Box 99"); hc.run()
check(not any(v.get("gone") for v in hc.state["CAP"].values()), "listado al tope: no marca desaparecidos")
# include_keywords por tienda
ocfg["sites"]=[{"name":"MIX","url":"https://m/x","type":"api","include_keywords":["one piece"]}]
mcat=[P(1,"One Piece box")]
hm=Harness(o,ocfg,lambda s:(s,list(mcat),None)); hm.run()
mcat+= [P(2,"Pokemon box"),P(3,"One Piece box 2")]
msgs=hm.run(); check(len(msgs)==1 and "Pokemon" not in msgs[0] and "box 2" in msgs[0], "include_keywords filtra")
# promo con sonido en OP, sin sonido en común
check(o.is_loud([{"promo":True}],ocfg) and not m.is_loud([{"promo":True}],base), "sound_for_promo")
# Woo: stock_text y backorder en el aviso
w=[{"id":5,"name":"OP case","permalink":"p","prices":{"price":"100","currency_symbol":"€"},"is_in_stock":True,"is_on_backorder":True,"stock_availability":{"text":"Solo quedan 1 disponibles"}}]
pp=o.extract_products_api(w,"https://w")[0]; pp.update(alert_type="new"); o.mark_priority(pp,ocfg)
txt="".join(o.format_notification("W","high",[pp],ocfg))
check("Solo quedan 1" in txt and "bajo pedido" in txt, "stock_text + backorder en el aviso")
# re-sync con resumen
ocfg["sites"]=[{"name":"RS","url":"https://r/x","type":"api"}]
rcat=[P(1,"OP Booster Box OP-15")]
hr=Harness(o,ocfg,lambda s:(s,list(rcat),None)); hr.run()
rcat.extend(P(300+i,f"OPCG Starter Deck ST-{i}") for i in range(25))
msgs=hr.run(); check(len(msgs)==1 and "re-sincronización" in msgs[0], f"re-sync: solo el resumen: {msgs}")
# --- arreglos del 06/10 ---
st={"__health__":{"X":{"fails":20,"alerted":False}}}
_,undo=m._collect_health_alerts(st,{"health_fail_threshold":10}); undo()
try: m._collect_health_alerts(st,{"health_fail_threshold":10}); check(True,"")
except TypeError: check(False,"last_digest None revienta")
from bs4 import BeautifulSoup
it=BeautifulSoup('<div class="p"><span>Más vendido</span><a class="add-to-cart">Añadir al carrito</a></div>',"html.parser").div
check(m.detect_html_stock_signal(it) is True, "'Más vendido' no es agotado")
# merch excluido después: sale del state y no cuenta como desaparecido
ocfg["sites"]=[{"name":"FG","url":"https://fg/x","type":"api"}]
fcat=[P(i,f"OP Booster Box {i}") for i in range(1,4)]+[P(50+i,f"Funko Luffy {i}") for i in range(8)]
hf=Harness(o,ocfg,lambda s:(s,list(fcat),None)); hf.run()
ocfg["sites"][0]["exclude_keywords"]=["funko"]; hf.run()
check(len(hf.state["FG"])==3, f"merch excluido sale del state: {len(hf.state['FG'])}")
gone=fcat.pop(0); hf.run()
check(hf.state["FG"][gone["uid"]].get("gone") is True, "con merch excluido, el desaparecido se detecta (no 'anómalo')")
# desaparición masiva persistente (AllInTCG): se acepta a la 3ª pasada y la vuelta avisa
ocfg["sites"]=[{"name":"AIT","url":"https://ait/x","type":"api"}]
acat=[P(i,f"OP Booster Box {i}") for i in range(1,13)]
ha=Harness(o,ocfg,lambda s:(s,list(acat),None)); ha.run()
quedan=acat[:1]; acat[:]=quedan
ha.run(); ha.run(); check(not any(v.get("gone") for v in ha.state["AIT"].values()), "2 pasadas: aún anómalo")
ha.run(); check(sum(1 for v in ha.state["AIT"].values() if v.get("gone"))==11, "3ª pasada: aceptado")
acat.append(P(5,"OP Booster Box 5")); msgs=ha.run(); check(len(msgs)==1 and "VUELVE" in msgs[0], "vuelve una caja retirada = RESTOCK")
# --- botón de cesta ---
d={"products":[{"id":1,"title":"x","handle":"x","variants":[{"id":11,"price":"1","available":False},{"id":12,"price":"1","available":True}]}]}
check(o.extract_products_api(d,"https://s.com/c/products.json")[0]["cart_url"]=="https://s.com/cart/12:1","Shopify: cesta con la variante disponible")
w=[{"id":7,"name":"y","permalink":"https://w.es/p/y/","prices":{"price":"100"},"is_in_stock":True,"type":"simple"},
   {"id":8,"name":"z","permalink":"https://w.es/p/z/","prices":{"price":"100"},"is_in_stock":True,"type":"variable"}]
ws=o.extract_products_api(w,"https://w.es")
check(ws[0]["cart_url"]=="https://w.es/p/y/?add-to-cart=7" and ws[1]["cart_url"]=="", "Woo: cesta solo en simples")
kb=o.cart_keyboard([({**ws[0],"alert_type":"new","high_value":True},None),({**ws[1],"alert_type":"new"},None)],ocfg)
check(kb and len(kb["inline_keyboard"])==1 and kb["inline_keyboard"][0][0]["url"].endswith("add-to-cart=7"), "teclado con un botón")
# --- cookie de JavaScript + _fields + caché ---
class Resp:
    def __init__(s,text="",j=None,ct="application/json"): s.text=text; s._j=j; s.headers={"Content-Type":ct}; s.status_code=200
    def raise_for_status(s): pass
    def json(s): return s._j
pedidas=[]
def fake_get(url,headers=None,timeout=None):
    pedidas.append((url,dict(headers or {})))
    if url.endswith(".es/"): return Resp("<script>document.cookie = 'dhd2=abc123; max-age=86400'</script>",ct="text/html")
    return Resp(j=[{"id":1,"name":"OP box","permalink":"https://f.es/p","prices":{"price":"1"},"is_in_stock":True}])
f=load("limpio")
f.requests.get=fake_get
_,pr,err=f.fetch_site({"name":"F","url":"https://f.es/wp-json/wc/store/v1/products?per_page=5","type":"api","cookie_challenge":True},ocfg)
api=[x for x in pedidas if "wp-json" in x[0]][0]
check(pr and api[1].get("Cookie")=="dhd2=abc123", "cookie del reto JS enviada")
check("_fields=" in api[0] and "_cb=" in api[0], "Woo con _fields y _cb")
# --- editar el aviso cuando se agota ---
ocfg["sites"]=[{"name":"ED","url":"https://ed.com/c/products.json?limit=250","type":"api"}]
ecat=[P(1,"OP Booster Box OP-15")]
he=Harness(o,ocfg,lambda s:(s,list(ecat),None)); he.run()
nuevo=P(2,"OP Booster Box OP-16"); nuevo["cart_url"]="https://ed.com/cart/22:1"
ecat.append(nuevo); msgs=he.run()
check(len(msgs)==1 and "OP-16" in str(he.state.get("__live__")), "producto avisado en stock queda registrado")
ecat[1]=dict(nuevo, in_stock=False); he.run()
check(len(he.edits)==1 and "❌" in he.edits[0][1] and "<s><b>OP Booster Box OP-16</b></s>" in he.edits[0][1], f"aviso editado como agotado: {he.edits}")
check(he.edits and he.edits[0][2]["inline_keyboard"]==[], "botón de cesta retirado")
check(not he.state.get("__live__"), "registro limpiado tras editar")
he.run(); check(len(he.edits)==1, "no se edita dos veces")
check(o._duracion(240)=="4 min" and o._duracion(3*3600+600)=="3 h 10 min", "formato de duración")
# --- webs oficiales: aviso de novedad y códigos 📅 ---
of=load("oficial")
pagina={"html":'<ul><li class="x"><a class="l" href="/p/op18.html"><h4 class="t">BOOSTER PACK [OP-18]</h4><time>November 20, 2099</time></a></li></ul>'}
of.requests.get=lambda url,headers=None,timeout=None: Resp(pagina["html"],ct="text/html")
env=[]; of.send_telegram=lambda t,c,msg,*a,**k:(env.append(msg),5)[1]
fcfg={"user_agent":"UA","bot_label":"OP","set_code_pattern":r"\b(OP|EB|PRB)-?(\d{2})\b",
      "official_sources":[{"name":"Oficial","url":"https://o.com/products/","selector":"li.x","title_selector":"h4.t","link_selector":"a.l","info_selector":"time"}]}
fst={}; of.check_official(fst,fcfg,"x","y")
check(env==[] and fcfg["_fresh_codes"]=={"OP-18"}, f"base oficial en silencio y OP-18 (sale en el futuro) prioritario: {fcfg.get('_fresh_codes')}")
pagina["html"]+='<ul><li class="x"><a class="l" href="/p/eb06.html"><h4 class="t">EXTRA BOOSTER [EB-06]</h4></a></li></ul>'
fst["__official__"]["last_check"]=0; of.check_official(fst,fcfg,"x","y")
check(len(env)==1 and "EB-06" in env[0] and "novedad oficial" in env[0], "producto oficial nuevo avisado")
check(fcfg["_fresh_codes"]=={"OP-18","EB-06"}, "su código pasa a prioritario")
q={"title":"One Piece EB06 [EN] Preventa"}; of.mark_priority(q,fcfg)
check(q["fresh_set"] and of.rank_mark(q)=="📅" and of.is_priority(q,fcfg), "listado vago con código fresco = 📅 prioritario")
check(of._fecha_oficial("Release Nov. 20, 2026") and of._fecha_oficial("sin fecha") is None, "fechas oficiales")
# --- reajuste masivo de inventario (Fridam, 06/10) ---
ocfg["sites"]=[{"name":"FR","url":"https://fr.com/c/products.json?limit=250","type":"api"}]
rcat=[P(i,f"OP Booster Box {i}",stock=False) for i in range(72)]
hr2=Harness(o,ocfg,lambda s:(s,list(rcat),None)); hr2.run()
for i in range(41): rcat[i]=P(i,f"OP Booster Box {i}",stock=True)
msgs=hr2.run()
check(len(msgs)==1 and "reajuste de inventario" in msgs[0] and "41 productos" in msgs[0], f"41 restocks de 72 = un resumen: {[m[:60] for m in msgs]}")
check(not hr2.state.get("__live__"), "el reajuste no registra avisos para editar")
ocfg["sites"]=[{"name":"BIG","url":"https://big.com/c/products.json?limit=250","type":"api"}]
bcat=[P(1000+i,f"OP Booster Box B{i}",stock=False) for i in range(240)]
hb=Harness(o,ocfg,lambda s:(s,list(bcat),None)); hb.run()
for i in range(20): bcat[i]=P(1000+i,f"OP Booster Box B{i}",stock=True)
msgs=hb.run(); check(len(msgs)==1 and "reajuste" not in msgs[0] and "VUELVE" in msgs[0], "20 restocks de 240 = avisos normales")
print(f"\n{ok} OK, {fail} FAIL")
sys.exit(1 if fail else 0)
