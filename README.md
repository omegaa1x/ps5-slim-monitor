# Monitor PS5 Slim ≤ 575 € (España) → Telegram

Comprueba tiendas españolas 24/7 desde GitHub Actions (sin tu PC) y te avisa por Telegram de cualquier
**PS5 Slim (Disco o Digital) nueva, en stock, con precio TOTAL entregado ≤ 575,00 €** (575,00 cumple; 575,01 no).

## Cómo funciona
- **cron-job.org** lanza el workflow `monitor` cada 5 min (`workflow_dispatch`). El temporizador propio de
  GitHub (`*/15`) es solo un respaldo.
- Cada ejecución: lee comandos de Telegram → comprueba las fuentes que tocan → evalúa → guarda → avisa.
- El estado (historial, avisos enviados, salud de fuentes) es un SQLite guardado en la rama **`state`**
  (un único commit que se sobrescribe). Si no se puede leer esa rama, la ejecución se aborta para no
  repetir avisos.

## Fuentes y fiabilidad
Comprobado desde los servidores de GitHub el 20/11/2026 (`tools/probe_sources.py`, workflow `probe-sources`).

| Fuente | Método | Estado |
|---|---|---|
| GAME | Listado público + schema.org de la ficha | ✅ Verificado. Envío no indicado → "no confirmado" (`SHIPPING_EUR_GAME`) |
| El Corte Inglés | Categoría PS5 (lista con precio/estado) + schema.org de la ficha | ✅ Verificado. Solo vendedor El Corte Inglés |
| Hipercor | Igual que El Corte Inglés | ✅ Verificado |
| MediaMarkt | Búsqueda + schema.org de la ficha | ⚠️ Funciona desde casa, pero bloquea (403) las IPs de GitHub; se reintenta |
| Chollometro, Blogdechollos, Ofertitas | RSS oficiales | ✅ Cubren Amazon, Carrefour, PcComponentes, Fnac… → avisos **⚠️ SIN VERIFICAR** |
| Amazon, Carrefour, PcComponentes, Fnac, Worten, Toys"R"Us | — | Bloqueo anti-bot/robots. No se elude. Solo vía webs de chollos |
| Xtralife, Alcampo, Lidl, Coolmod | — | Precios cargados con JavaScript (sin datos legibles) |

Una misma oferta publicada en varias webs de chollos (o ya avisada por la tienda) solo se avisa una vez.

`python -m ps5monitor sources` muestra esta tabla.

## Calendario
- **Pico (máxima prioridad)**: `[2026-11-23 00:00, 2026-12-01 00:00)` Europe/Madrid → incluye completos el
  lunes 23 y el lunes 30 (Cyber Monday). Intervalos pico: Chollometro 2 min, GAME/MediaMarkt 3 min.
- **Normal (resto, 24/7)**: Chollometro 10 min, GAME/MediaMarkt 15 min.
- En GitHub Actions el mínimo real es el disparador (~5 min) + ~1 min de ejecución. Los cambios de modo son
  automáticos y se recalculan en cada ejecución (timestamps en UTC, mensajes en hora de Madrid).
- **Retrasos**: intervalo de sondeo ≠ retraso de detección (≈ intervalo + 1 min) ≠ entrega Telegram (segundos)
  ≠ disponibilidad de GitHub/cron-job.org. No hay detección instantánea.

## Avisos
- Primera ejecución: resumen con **todas** las ofertas que ya cumplen (ordenadas de más barata a más cara).
- Después: un aviso por oferta nueva; repetición solo si baja ≥ 5 € más (o marca nuevo mínimo global), si
  vuelve a estar en stock, o si vuelve a bajar del límite. Si hay muchas a la vez → resumen.
- Avisos de salud: fuente caída > 30 min (pico) / 2 h (normal), huecos sin ejecuciones, cambio de modo y
  un "💓 latido" diario a las 09:00. **Si un día no recibes el latido, algo falla.**
- Se rechazan: Pro, PS5 original, accesorios, PS4, Portal, reacondicionados/usados, financiación (€/mes),
  precios con entrega de consola, precios solo socios, "desde X €", vendedores de marketplace, sin stock.

## Comandos de Telegram (solo desde tu chat; respuesta en ≤ ~5 min)
`/status` · `/deals` · `/pause` · `/resume` · `/threshold 550` · `/threshold reset` · `/test`

## Cambiar el límite
Una línea en `.github/workflows/monitor.yml`: `MAX_TOTAL_PRICE_EUR: "575.00"` (o `/threshold` en Telegram).

## Uso local (opcional)
```powershell
uv venv .venv; uv pip install -e ".[dev]"
.venv\Scripts\python -m ps5monitor --env-file "$env:USERPROFILE\.ps5monitor\secrets.env" test-telegram
.venv\Scripts\python -m ps5monitor scan-once --dry-run      # no envía nada, imprime los mensajes
.venv\Scripts\python -m ps5monitor status
.venv\Scripts\python -m pytest -q
```
`run` ejecuta un bucle continuo (para un servidor siempre encendido, con intervalos de hasta 2–3 min).

## Solución de problemas
| Síntoma | Causa / solución |
|---|---|
| No llega nada | Revisa que el job de cron-job.org esté **activado** y su historial (respuesta 204 = OK). Mira *Actions* en GitHub. |
| cron-job.org da 401/403/404 | Token caducado o mal pegado (`Bearer github_pat_...`), permiso *Actions: Read and write*, o URL/usuario incorrectos. |
| Aviso "fuente no responde" | La tienda bloquea o falla. Las demás siguen. Si persiste días, la web cambió: revisar el adaptador. |
| "Hueco de cobertura" | GitHub/cron-job.org no lanzaron ejecuciones. Normalmente se recupera solo. |
| Quiero empezar de cero | Borra la rama `state` en GitHub (perderás el historial y se reenviará el resumen inicial). |
| Mensajes duplicados | Solo puede ocurrir si falla el guardado del estado tras enviar (entrega "al menos una vez"). |
| Token de Telegram filtrado | `/revoke` en @BotFather y actualiza el secreto: `gh secret set TELEGRAM_BOT_TOKEN`. |
| El workflow se desactivó | GitHub desactiva *schedules* tras 60 días sin actividad; los disparos de cron-job.org siguen funcionando. Reactívalo en *Actions*. |

## Límites honestos
- GitHub Actions no es un servidor: los disparos pueden retrasarse y las condiciones de GitHub dicen que
  Actions es para compilar/probar proyectos; GitHub podría desactivarlo.
- Las IPs de GitHub (centros de datos en EE. UU.) pueden ser bloqueadas por tiendas en cualquier momento;
  el monitor lo detecta y avisa, nunca lo interpreta como "sin stock".
- Coste: 0 € (repositorio público, cron-job.org gratis). El código y el historial de precios son públicos;
  los secretos no.
