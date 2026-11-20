"""Source registry with documented method and reliability status (checked from GitHub's runners, 2026-11-20)."""

from __future__ import annotations

SUPPORTED = {
    "game": "Listado público + schema.org de la ficha (precio, stock, estado, vendedor). Envío: se lee de la "
            "ficha si aparece; si no, 'no confirmado' (SHIPPING_EUR_GAME para fijarlo).",
    "elcorteingles": "Categoría pública de consolas PS5 (lista de productos con precio/estado) + schema.org de la "
                     "ficha. Solo vendedor El Corte Inglés.",
    "hipercor": "Igual que El Corte Inglés (mismo grupo y misma web).",
    "mediamarkt": "Búsqueda + schema.org de la ficha. Funciona desde una IP doméstica, pero BLOQUEA (403) las IPs "
                  "de GitHub: se reintenta con espera creciente y avisa si sigue caída.",
    "chollometro": "RSS oficial (/rss/nuevos). Cubre todas las tiendas (Amazon, Carrefour, PcComponentes, Fnac...). "
                   "Precios publicados por usuarios: avisos SIN VERIFICAR.",
    "blogdechollos": "RSS oficial (/feed/). Ofertas publicadas por editores: avisos SIN VERIFICAR.",
    "ofertitas": "RSS oficial (/feed/). Ofertas publicadas por editores: avisos SIN VERIFICAR.",
}

UNSUPPORTED = {
    "amazon_es": "Desafío anti-bot y sus Condiciones de Uso prohíben robots; la API de afiliados exige cuenta "
                 "aprobada. Cubierto vía webs de chollos.",
    "carrefour": "Cloudflare 403 (anti-bot). Cubierto vía webs de chollos.",
    "pccomponentes": "Cloudflare 403 (anti-bot). Cubierto vía webs de chollos.",
    "fnac": "403 y robots.txt prohíbe las búsquedas. Cubierto vía webs de chollos.",
    "worten": "403 y robots.txt prohíbe las búsquedas. Cubierto vía webs de chollos.",
    "toysrus": "HTTP 451 desde GitHub (bloqueado). Cubierto vía webs de chollos.",
    "xtralife": "Accesible, pero los precios se cargan con JavaScript (no hay datos en la página).",
    "alcampo / lidl / coolmod": "Accesibles, pero catálogo cargado con JavaScript; sin datos legibles.",
    "playstation_direct": "No vende en España (redirige a playstation.com).",
    "tien21 / euronics / milar / mielectro / electrocosto": "Sin consolas PS5 localizables en su web pública.",
}
