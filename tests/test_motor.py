import sys, os, json, importlib.util, copy, time
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
check(len(he.edits)==0, "una sola lectura agotado: aún no se edita")
he.run()
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
rcat=[P(i,f"OP Starter Deck {i}",stock=False) for i in range(72)]
hr2=Harness(o,ocfg,lambda s:(s,list(rcat),None)); hr2.run()
for i in range(41): rcat[i]=P(i,f"OP Starter Deck {i}",stock=True)
msgs=hr2.run()
check(len(msgs)==1 and "reajuste de inventario" in msgs[0] and "41 productos" in msgs[0], f"41 restocks flojos de 72 = un resumen: {[m[:60] for m in msgs]}")
check(not hr2.state.get("__live__"), "el reajuste no registra avisos para editar")
# 24 preventas del 30 aniv abiertas a la vez: las 16 prioritarias SÍ avisan
pcfg=copy.deepcopy(base); pcfg.update(required_keywords=["30th"],top_priority_keywords=["booster box"],high_value_keywords=["elite trainer box"],exclude_keywords=[])
pcfg["sites"]=[{"name":"PV","url":"https://pv.com/c/products.json?limit=250","type":"api"}]
tit=["30th Booster Box"]*8+["30th Elite Trainer Box"]*8+["30th Booster Pack"]*8
pv=[P(500+i,f"{t} {i}",stock=False) for i,t in enumerate(tit)]
mp=load("preventa")
hp=Harness(mp,pcfg,lambda s:(s,list(pv),None)); hp.run()
pv[:]=[dict(x,in_stock=True) for x in pv]; msgs=hp.run(); txt="".join(msgs)
check(sum(txt.count(f"30th Booster Box {i}") for i in range(8))==8 and sum(txt.count(f"30th Elite Trainer Box {i}") for i in range(8,16))==8,
      "preventa abierta entera: las 16 prioritarias se avisan con normalidad")
check(any("reajuste" in x for x in msgs) and not any("30th Booster Pack 20" in x and "VUELVE" in x for x in msgs), "los 8 sobres van al resumen")
# el mismo producto se repone otra vez en menos de 1 h: marcado y sin sonido
pv[0]=dict(pv[0],in_stock=False); hp.run(); pv[0]=dict(pv[0],in_stock=True); msgs=hp.run()
check(len(msgs)==1 and "(otra vez)" in msgs[0] and not m.is_loud([dict(pv[0],repeat=True,top_priority=True)],pcfg), "restock repetido: marcado y sin sonido")
# producto publicado hace 200 días que aparece agotado: sin aviso
viejo=P(900,"30th Booster Box vieja",stock=False); viejo["published"]=time.time()-200*86400
pv.append(viejo); check(hp.run()==[], "producto viejo agotado que aparece: sin aviso")
viejo2=P(901,"30th Booster Box vieja 2",stock=True); viejo2["published"]=time.time()-200*86400
pv.append(viejo2); msgs=hp.run(); check(len(msgs)==1 and "VUELVE" in msgs[0] and "NUEVO" not in msgs[0], "producto viejo en stock: 'vuelve', no 'nuevo'")
ocfg["sites"]=[{"name":"BIG","url":"https://big.com/c/products.json?limit=250","type":"api"}]
bcat=[P(1000+i,f"OP Booster Box B{i}",stock=False) for i in range(240)]
hb=Harness(o,ocfg,lambda s:(s,list(bcat),None)); hb.run()
for i in range(20): bcat[i]=P(1000+i,f"OP Booster Box B{i}",stock=True)
msgs=hb.run(); check(len(msgs)==1 and "reajuste" not in msgs[0] and "VUELVE" in msgs[0], "20 restocks de 240 = avisos normales")
# --- paginación de colecciones Shopify llenas ---
pg=load("pagina")
def shop_items(a,b): return [{"id":i,"title":f"x{i}","handle":f"h{i}","variants":[{"id":i,"price":"1","available":True}]} for i in range(a,b)]
paginas={"1":shop_items(0,250),"2":shop_items(250,264)}
def fake_pg(url,headers=None,timeout=None):
    n=url.split("page=")[1] if "page=" in url else "1"
    return Resp(j={"products":paginas.get(n,[])})
pg.requests.get=fake_pg
sc={"name":"S","url":"https://s.com/collections/x/products.json?limit=250","type":"api"}
_,prs,_=pg.fetch_site(sc,{"user_agent":"UA"})
check(len(prs)==264 and sc.get("_cap") is None, f"colección de 264: se leen las 2 páginas ({len(prs)})")
# --- 429 de Shopify: sin reintento y pausa tras 3 tiendas ---
rl=load("limite"); llamadas=[]
class R429:
    status_code=429; headers={}; text=""
rl.requests.get=lambda url,headers=None,timeout=None:(llamadas.append(url),R429())[1]
rl._RL.update(n=0,pausa=False)
for k in range(5):
    _,pr,err=rl.fetch_site_serial({"name":f"T{k}","url":f"https://t{k}.com/collections/x/products.json?limit=250","type":"api"},{"user_agent":"UA"})
check(len(llamadas)==3 and rl._RL["pausa"] and str(err).startswith(rl.RATE_LIMIT_ERR), f"429: sin reintento y pausa tras 3 tiendas ({len(llamadas)} peticiones)")

# --- grupo de IMPORTANTES (vip) ---
v=load("vip")
check(v.price_eur("107.95€")==107.95 and abs(v.price_eur("100.00£")-116)<0.01 and v.price_eur("€1.299,95")==1299.95
      and v.price_eur("1,299.95$")==1299.95*0.86 and v.price_eur("Precio no disponible") is None, "price_eur: formatos y divisas")
check(v.detect_lang("One Piece OP-14 Booster Box Japonés")=="jp" and v.detect_lang("OP14 Box (JP)")=="jp"
      and v.detect_lang("OP14 Booster Box EN")=="en" and v.detect_lang("Caja OP14 en preventa") is None
      and v.detect_lang("OP14 Box", "Sunny Store (Cajas OP JAP)")=="jp" and v.detect_lang("OP14 Box Chinese")=="otro"
      and v.detect_lang("Booster Box Machine") is None, "detect_lang: jp/en/otro/None, 'en' preposición no cuenta")
vcfg={"priority_exclude":["card case"],"vip":{"promos":True,"rules":[
    {"label":"Case","keywords":["case","carton"],"exclude":["dice"],"min_eur":300},
    {"label":"Box JP","keywords":["booster box","box"],"exclude":["illustration"],"lang":"jp","max_eur":80},
    {"label":"Box EN","keywords":["booster box","box"],"exclude":["illustration"],"lang":"en","max_eur":200},
    {"label":"UPC 30th","keywords":["ultra premium"],"require":["30th","30 aniversario"]},
    {"label":"UPC","keywords":["ultra premium"],"max_eur":200}]}}
def A(t,price="100€",stock=True,promo=False): return {"title":t,"price":price,"in_stock":stock,"promo":promo,"alert_type":"new","uid":t}
vm=lambda t,**k: v.vip_match(A(t,**k),"Tienda",vcfg)
check(vm("OP15 Booster Case EN",price="2500€")=="Case" and vm("OP15 Booster Case EN",price="Precio no disponible")=="Case", "case: cualquier precio")
check(vm("OP15 Case",price="150€") is None and vm("Official Dice and Dice Case",price="900€") is None, "case: mínimo 300€ y exclude")
check(vm("Card Case One Piece",price="900€")is None, "priority_exclude manda: 'card case' no es un case")
check(vm("Showcase OP15") is None, "keyword como palabra entera ('showcase' no es case)")
check(vm("OP15 Booster Box JP",price="79€")=="Box JP" and vm("OP15 Booster Box JP",price="95€") is None, "box JP: máximo 80")
check(vm("OP15 Booster Box",price="180€")=="Box EN" and vm("OP15 Booster Box EN",price="230€") is None, "box sin idioma cuenta como EN; máximo 200")
check(vm("OP15 Booster Box Chinese",price="50€") is None, "otro idioma no entra")
check(vm("OP15 Booster Box",price="Precio no disponible")=="Box EN", "sin precio legible: entra (dudoso)")
check(vm("Illustration Box Vol 3",price="30€") is None, "exclude de la regla")
check(vm("Ultra Premium Collection 30th",price="600€")=="UPC 30th" and vm("Ultra Premium Collection Mega",price="250€") is None
      and vm("Ultra Premium Collection Mega",price="190€")=="UPC", "UPC: 30th sin límite, el resto hasta 200")
check(vm("Saikyo Jump promo",price="900€",promo=True)=="promo", "promos sin límite")
check(vm("OP15 Booster Case",stock=False,price="1200€") is None, "agotado no va al grupo")
check(v.vip_match(A("Naruto llavero",stock=False),"T",{"vip":{"all":True,"include_sold_out":True}})=="todo", "vip.all + include_sold_out")
_,wp,_=(None,v.extract_products_api([{"id":1,"name":"OP06 JP Box","permalink":"https://s/p","is_in_stock":True,
    "prices":{"price":"21305","currency_symbol":"¥","currency_minor_unit":0}},{"id":2,"name":"x","permalink":"https://s/q",
    "is_in_stock":True,"prices":{"price":"10995","currency_symbol":"€","currency_minor_unit":2}}]),None)
check(wp[0]["price"]=="21305¥" and wp[1]["price"]=="109.95€", f"Woo: yenes sin decimales ({wp[0]['price']}, {wp[1]['price']})")
# run_once: copia al grupo, el general sin sonido si ya no le queda nada gordo, agotado editado en los dos
os.environ["TELEGRAM_VIP_CHAT_ID"]="VIP"
vb=dict(base, silent_first_run=True, priority_exclude=[], high_value_keywords=["booster box","case"],
        vip={"rules":[{"label":"Box","keywords":["booster box"],"max_eur":200}]})
vcat={"A":[P(1,"Naruto starter",stock=True)]}
hv=Harness(v,vb,lambda site:(site,list(vcat["A"]),None))
envios=[]
def fake_send(tok,chat,msg,silent=False,reply_markup=None,**k):
    envios.append((chat,msg,silent)); hv._mid[0]+=1; return hv._mid[0]
v.send_telegram=fake_send
hv.run()
vcat["A"]=[P(1,"Naruto starter",stock=True),P(2,"Naruto booster box",stock=True),P(3,"Naruto booster box premium",stock=True)]
vcat["A"][2]["price"]="250€"
envios.clear(); hv.run()
vip_m=[e for e in envios if e[0]=="VIP"]; gen=[e for e in envios if e[0]!="VIP"]
check(len(vip_m)==1 and "IMPORTANTE" in vip_m[0][1] and "Naruto booster box</b>" in vip_m[0][1] and "premium" not in vip_m[0][1] and not vip_m[0][2],
      "al grupo va solo la box dentro del máximo, con sonido")
check(len(gen)==1 and "premium" in gen[0][1] and not gen[0][2], "general: lo recibe todo y suena (queda la box de 250€ fuera del grupo)")
vcat["A"]=[P(1,"Naruto starter",stock=True),P(2,"Naruto booster box",stock=False),P(3,"Naruto booster box premium",stock=True)]
vcat["A"][2]["price"]="250€"
hv.edits.clear(); hv.run(); hv.run()
check(len(hv.edits)==2 and all("agotado" in e[1] for e in hv.edits), f"agotado: se edita en el general Y en el grupo ({len(hv.edits)} ediciones)")
# Solo lo de importantes es gordo -> el general llega en silencio
vb2=dict(vb, high_value_keywords=["booster box"])
vcat["A"]=[P(10,"Naruto starter X",stock=True)]
hs=Harness(v,vb2,lambda site:(site,list(vcat["A"]),None)); v.send_telegram=fake_send; hs.run()
vcat["A"].append(P(11,"Naruto booster box 2",stock=True)); envios.clear(); hs.run()
check([e[0] for e in envios]==["VIP","y"] and envios[1][2], "si lo gordo ya fue al grupo, el general llega en silencio")
# Si el general falla, el grupo no se repite en la siguiente pasada
vcat["A"].append(P(12,"Naruto booster box 3",stock=True))
def falla_general(tok,chat,msg,silent=False,reply_markup=None,**k):
    envios.append((chat,msg,silent))
    if chat!="VIP": return False
    hs._mid[0]+=1; return hs._mid[0]
v.send_telegram=falla_general; envios.clear(); hs.run()
v.send_telegram=fake_send; envios.clear(); hs.run()
check([e[0] for e in envios]==["y"], f"reintento del general sin repetir el grupo ({[e[0] for e in envios]})")
del os.environ["TELEGRAM_VIP_CHAT_ID"]
print(f"\n{ok} OK, {fail} FAIL")
sys.exit(1 if fail else 0)
