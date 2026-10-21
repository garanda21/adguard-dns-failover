# adguard-dns-failover

Cambia automáticamente los DNS de tus instancias de **AdGuard Home** y/o **Pi-hole v6** a unos DNS alternativos cuando se detectan bloqueos masivos de IPs (por ejemplo, de rangos de Cloudflare), y restaura tu configuración original cuando el bloqueo termina.

Puedes usar el servicio DNS que quieras. Por defecto se usa [xdp.es](https://xdp.es), que devuelve IPs de Cloudflare no bloqueadas.

## Cómo funciona

1. Según los cron configurados, consulta el estado de bloqueos en [hayahora.futbol](https://hayahora.futbol/estado/data.json).
2. Por cada operador (DIGI, Movistar, Orange…) cuenta las IPs bloqueadas, como hace [parrazam/monitor-hay-futbol](https://github.com/parrazam/monitor-hay-futbol). Hay bloqueo si algún operador tiene al menos `BLOCK_MIN_IPS` IPs bloqueadas (20 por defecto) o supera `BLOCK_THRESHOLD_PCT` %. Se usa un número absoluto porque se monitorizan miles de IPs por operador: fuera de los partidos hay de 0 a 6 bloqueadas, y durante un bloqueo, entre 150 y 900 (solo un 4-10 %).
3. **Si hay bloqueo**, en cada instancia de AdGuard:
   - guarda una copia de `upstream_dns`, `bootstrap_dns`, `fallback_dns` y `upstream_mode` en `/data/backups/instance_<n>.json`;
   - pone los upstream, bootstrap y fallback alternativos (por defecto, xdp.es);
   - limpia la caché DNS para que el cambio sea inmediato.
4. **Cuando acaba el bloqueo**, restaura la configuración guardada y vuelve a limpiar la caché.
5. Opcionalmente, avisa por **Telegram** al empezar y al terminar el bloqueo, indicando qué instancias se han actualizado.

Si la consulta de bloqueos falla, no se toca nada. El backup nunca se sobrescribe mientras el bloqueo sigue activo.

## Instalación rápida

La imagen está publicada en Docker Hub ([`garanda21/adguard-dns-failover`](https://hub.docker.com/r/garanda21/adguard-dns-failover)) para `amd64` y `arm64` (Raspberry Pi incluida).

```bash
mkdir adguard-dns-failover && cd adguard-dns-failover
curl -fsSLO https://raw.githubusercontent.com/garanda21/adguard-dns-failover/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/garanda21/adguard-dns-failover/main/.env.example -o .env
# edita .env con tus instancias
docker compose up -d
docker compose logs -f
```

Para actualizar: `docker compose pull && docker compose up -d`.

Etiquetas disponibles: `latest` (última versión estable), `X.Y.Z` / `vX.Y.Z` (versión concreta) y `development` (pre-releases).

Si prefieres construir la imagen tú mismo, clona el repo, descomenta `build: .` en `docker-compose.yml` y ejecuta `docker compose up -d --build`.

## Configuración (`.env`)

### Instancias de AdGuard Home

AdGuard Home solo admite autenticación básica (usuario y contraseña), sin API keys. Añade una instancia por número:

```env
ADGUARD_1_URL=http://192.168.1.2:3000
ADGUARD_1_NAME=Casa          # opcional, nombre que aparece en Telegram
ADGUARD_1_USER=admin
ADGUARD_1_PASSWORD=mi_password

ADGUARD_2_URL=https://adguard.midominio.es
ADGUARD_2_USER=admin
ADGUARD_2_PASSWORD_FILE=/run/secrets/adguard_2_password   # alternativa con Docker secrets
```

La URL es la de la interfaz web (la misma con la que entras al panel).

### Instancias de Pi-hole (v6)

Se pueden mezclar con las de AdGuard. Solo es compatible **Pi-hole v6**, porque la v5 no tiene API para cambiar los DNS:

```env
PIHOLE_1_URL=http://192.168.1.10
PIHOLE_1_NAME=Pi-hole NAS
PIHOLE_1_PASSWORD=app-password
```

- **Contraseña:** usa una *app password* (en Pi-hole: Settings → Web interface / API → *Configure app password*). Si tu Pi-hole no tiene contraseña, déjala vacía.
- **Permiso para cambiar la configuración:** por defecto, las *app passwords* no pueden modificar ajustes (error `webserver.api.app_sudo is false`). Actívalo en Settings → All settings → Webserver and API → `webserver.api.app_sudo` (en modo *Expert*), o añade `FTLCONF_webserver_api_app_sudo: "true"` al compose de Pi-hole. También puedes usar la contraseña principal en lugar de una *app password*.
- **Solo DNS sin cifrar:** Pi-hole no admite DoH ni DoT como upstream, solo IPs (`IP` o `IP#puerto`). Durante el bloqueo se usa `BLOCK_PIHOLE_UPSTREAMS`, que por defecto son las IPs de xdp.es. Pi-hole no tiene bootstrap ni fallback.
- **No definas los upstreams con variables de entorno.** Si tu contenedor de Pi-hole tiene `FTLCONF_dns_upstreams`, esa opción es de solo lectura y la API no puede cambiarla. La app lo detecta, avisa y no toca nada. Quita la variable y configura los upstreams desde la web.
- **Caché:** para vaciar la caché se reinicia el resolver de Pi-hole (`restartdns`). Durante uno o dos segundos no resuelve.

### Programación

Varias expresiones cron separadas por `;`. Se ejecuta cada vez que salte cualquiera:

```env
# L-V cada 4 horas, S-D cada 2 horas
CRON_SCHEDULES=0 */4 * * 1-5;0 */2 * * 6,0
RUN_ON_START=true
TZ=Europe/Madrid
```

### Notificaciones por Telegram (opcional)

1. Crea un bot con [@BotFather](https://t.me/BotFather) y copia el token.
2. Escribe cualquier mensaje a tu bot (o añádelo a un grupo).
3. Consigue tu chat id, por ejemplo con [@userinfobot](https://t.me/userinfobot) o en `https://api.telegram.org/bot<TOKEN>/getUpdates`.

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789        # varios separados por ';'
```

Prueba la conexión con `docker compose exec adguard-dns-failover python -m app test-telegram`.

Solo se envía mensaje cuando cambia el estado (empieza o acaba el bloqueo) o si alguna instancia falla al cambiar.

### Operadores

Por defecto se tienen en cuenta todos los operadores (DIGI, Masmovil, Movistar, Orange, Vodafone). Si solo te interesa el tuyo, indícalo para que un bloqueo en otro operador no cambie tus DNS:

```env
ISPS=movistar
# o varios: ISPS=movistar,digi
```

Si escribes un operador que no aparece en los datos, se muestra un aviso en el log y se ignora.

### DNS durante el bloqueo

Por defecto se usa xdp.es, pero puedes poner cualquier DNS que admita AdGuard Home (DoH, DoT, DoQ o IP):

```env
BLOCK_UPSTREAMS=https://dns.xdp.es/dns-query;tls://dns.xdp.es
BLOCK_BOOTSTRAP=85.208.114.51;51.170.52.10
```

El bootstrap solo sirve para resolver el nombre de los upstream (por ejemplo, `dns.xdp.es`). Usa IPs del mismo proveedor.

### Todas las variables

| Variable | Por defecto | Descripción |
|---|---|---|
| `ADGUARD_<n>_URL` | — | URL de cada instancia (obligatoria, al menos una) |
| `ADGUARD_<n>_USER` / `_PASSWORD` | — | Credenciales. Admite `_USER_FILE` / `_PASSWORD_FILE` |
| `ADGUARD_<n>_NAME` | host de la URL | Nombre en las notificaciones |
| `TELEGRAM_BOT_TOKEN` | — | Token del bot (admite `_FILE`) |
| `TELEGRAM_CHAT_ID` | — | Chat id(s) separados por `;` |
| `PIHOLE_<n>_URL` / `_PASSWORD` / `_NAME` | — | Instancias de Pi-hole v6 (admite `_PASSWORD_FILE`) |
| `CRON_SCHEDULES` | `*/30 * * * *` | Crons separados por `;` |
| `RUN_ON_START` | `true` | Comprobar al arrancar el contenedor |
| `TZ` | `Europe/Madrid` | Zona horaria de los cron |
| `BLOCK_MIN_IPS` | `20` | Nº de IPs bloqueadas en un operador para considerar bloqueo |
| `ISPS` | (todos) | Operadores a vigilar, separados por comas (`movistar,digi`). No distingue mayúsculas. Vacío = todos |
| `BLOCK_THRESHOLD_PCT` | `50` | % de IPs bloqueadas en un operador para considerar bloqueo |
| `API_URL` | `https://hayahora.futbol/estado/data.json` | Fuente de datos |
| `BLOCK_UPSTREAMS` | `https://dns.xdp.es/dns-query;tls://dns.xdp.es` | Upstreams durante el bloqueo |
| `BLOCK_BOOTSTRAP` | `85.208.114.51;51.170.52.10` | Bootstrap durante el bloqueo |
| `BLOCK_FALLBACK` | igual que `BLOCK_UPSTREAMS` | Fallback DNS durante el bloqueo (`none` = no tocarlo) |
| `BLOCK_PIHOLE_UPSTREAMS` | `85.208.114.51;51.170.52.10` | Upstreams de Pi-hole durante el bloqueo (solo IP o `IP#puerto`) |
| `BLOCK_UPSTREAM_MODE` | (no se cambia) | `load_balance`, `parallel` o `fastest_addr` |
| `DRY_RUN` | `false` | Solo muestra lo que haría, sin cambiar nada |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`… |

## Problemas frecuentes

**La web sigue sin cargar justo después del cambio.** Tus dispositivos guardan en caché la IP bloqueada durante el TTL que tenía la respuesta, normalmente hasta 5 minutos con Cloudflare. Reiniciar AdGuard no ayuda, porque la caché está en el dispositivo. Puedes esperar un poco o vaciarla:

- **macOS:** `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`
- **iPhone/iPad:** activa y desactiva el modo avión.
- **Chrome:** `chrome://net-internals/#dns` → *Clear host cache*, y en `#sockets` → *Flush socket pools*.

**Recomendado:** en cada AdGuard, en *Ajustes → Configuración DNS → Caché*, pon el **TTL máximo en `60`**. Así ningún dispositivo guarda una respuesta más de un minuto y los cambios se notan casi al momento.

**Algunas webs cargan y otras se quedan "pensando".** Comprueba que tus dispositivos usan de verdad AdGuard. Mira el *Registro de consultas* de AdGuard y revisa estos casos:

- **DNS seguro del navegador (Chrome, Firefox, Edge):** se salta AdGuard. Desactívalo.
- **iCloud Private Relay y "Limitar rastreo de dirección IP" (iOS/macOS):** parte del tráfico de Safari sale por los servidores de Apple, que pueden estar en IPs bloqueadas. Para desactivarlo en toda la red, añade estas reglas personalizadas en AdGuard:
  ```
  ||mask.icloud.com^
  ||mask-h2.icloud.com^
  ||mask-canary.icloud.com^
  ||mask.apple-dns.net^
  ```
- **El router anuncia un DNS IPv6 del operador:** los dispositivos lo usan en lugar de AdGuard. Desactívalo en el router o haz que anuncie AdGuard.
- **Datos móviles:** con datos móviles no se pasa por AdGuard.

**Al terminar un bloqueo no se restauró mi configuración.** La app solo restaura si guardó un backup (`./data/backups/`). Si ya tenías los DNS alternativos puestos a mano al arrancarla, no hay nada que restaurar: vuelve a poner tus DNS a mano.

## Comandos útiles

```bash
docker compose exec adguard-dns-failover python -m app status     # estado del bloqueo e instancias
docker compose exec adguard-dns-failover python -m app run-once   # forzar una comprobación
docker compose exec adguard-dns-failover python -m app restore    # restaurar DNS originales ya
docker compose exec adguard-dns-failover python -m app test-telegram  # mensaje de prueba
```

Los backups se guardan en `./data/backups/`. Tras restaurar, el último queda como `instance_<n>.restored.json`.

## Desarrollo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Publicar una versión

Las versiones se publican al subir una etiqueta `vX.Y.Z` que apunte a un commit de `main`. La GitHub Action pasa los tests, publica la imagen multiplataforma en Docker Hub y crea la release en GitHub:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

Las etiquetas con sufijo (`v1.1.0-beta.1`) se publican como pre-release con la etiqueta Docker `development`. El repo necesita los secrets `DOCKERHUB_USERNAME` y `DOCKERHUB_TOKEN`.

## Créditos

- Detección basada en [parrazam/monitor-hay-futbol](https://github.com/parrazam/monitor-hay-futbol) y los datos de [hayahora.futbol](https://hayahora.futbol).
- DNS por defecto de [xdp.es](https://xdp.es).

## Licencia

MIT
