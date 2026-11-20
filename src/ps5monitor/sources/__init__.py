"""Source registry with documented method and reliability status."""

from __future__ import annotations

SUPPORTED = {
    "chollometro": "RSS oficial (/rss/nuevos). Cubre todas las tiendas (Amazon, El Corte Inglés, Fnac...), "
                   "pero los precios los publican usuarios: avisos marcados SIN VERIFICAR.",
    "game": "Listado público + datos estructurados schema.org de la página de producto (precio, stock, "
            "estado, vendedor). Envío: se lee de la página si aparece; si no, 'no confirmado'.",
    "mediamarkt": "Búsqueda pública + schema.org de la página de producto (precio, stock, vendedor, envío). "
                  "Vendedores de marketplace excluidos por defecto.",
}

UNSUPPORTED = {
    "amazon_es": "Las Condiciones de Uso de Amazon prohíben robots/extracción de datos; PA-API 5 se retiró en "
                 "2026 y la Creators API exige una cuenta de Afiliados aprobada. Cubierto solo vía Chollometro.",
    "pccomponentes": "Cloudflare devuelve un desafío anti-bot (HTTP 403). No se elude. Solo vía Chollometro.",
    "carrefour": "Cloudflare 403 y robots.txt prohíbe las búsquedas. Solo vía Chollometro.",
    "elcorteingles": "'Access Denied' (403) incluso en robots.txt. Solo vía Chollometro.",
    "fnac": "Protección anti-bot (403). Solo vía Chollometro.",
}
