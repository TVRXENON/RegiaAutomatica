# Regia automatica TVR Xenon

Programma di messa in onda automatica: legge i video dalla CDN di Bunny secondo il palinsesto di ogni giorno e li trasmette in RTMP verso il server dell'emittente, 24 ore su 24. Si gestisce da una pagina web, da computer o da telefono.

Il programma è un solo container Docker con due parti. La prima è la pagina di gestione (In onda, Palinsesto, Storico, Impostazioni, Utenti). La seconda è il motore di messa in onda, che usa ffmpeg.

## Come trasmette

I video non vengono ricodificati né copiati: il motore li legge dall'indirizzo sulla CDN e li passa così come sono al server RTMP. Il collegamento RTMP resta aperto da un video all'altro e i tempi del flusso restano continui, quindi il server riceve un'unica trasmissione senza interruzioni.

Per questo **tutti i video devono avere lo stesso formato**: H.264, 1920×1080, 25 fotogrammi al secondo, audio AAC stereo a 48.000 Hz, bitrate non oltre 2.500 kbps. Il modo più semplice è esportarli tutti con lo stesso preset, a bitrate costante o con un massimo di 2,5 Mbit/s. Ogni video viene controllato quando lo si aggiunge al palinsesto. Quelli diversi vengono segnalati in rosso e al momento della messa in onda vengono saltati, così non fanno cadere lo stream. I valori richiesti si cambiano in Impostazioni, Formato dei video.

## Regole del palinsesto

La giornata parte alle 00:00 e i video vanno in onda uno dopo l'altro. La pagina mostra per ciascuno l'ora di inizio, l'ora di fine e la durata.

Se un video è ancora in onda a mezzanotte, finisce e poi parte il palinsesto del giorno dopo, con un piccolo ritardo che la pagina segnala. I video che inizierebbero dopo la mezzanotte non vanno in onda: la pagina li indica come "oltre la mezzanotte".

Quando la playlist finisce prima di mezzanotte, o in un giorno senza palinsesto, va in onda in loop il video di riempimento. I video aggiunti alla giornata mentre va il riempitivo partono alla fine del giro di riempitivo in corso.

Le modifiche al palinsesto valgono fino a circa 15 secondi prima della messa in onda di ciascun video: è l'anticipo con cui il motore accoda il video successivo.

## Comportamenti automatici

**Ripresa dopo un'interruzione.** Se il collegamento cade, se il programma si riavvia o se la trasmissione viene fermata e poi riavviata, il motore riparte allineato all'orario. Riprende il video che doveva essere in onda in quel momento, dal punto giusto. I video che nel frattempo sarebbero dovuti andare in onda vengono saltati e segnati come "non trasmesso".

**Errori di collegamento.** Il motore riprova da solo, con attese crescenti fino a 30 secondi. Lo stato è visibile nella pagina In onda.

**Schermo nero.** Se manca il video di riempimento, o non si apre, nei vuoti va in onda uno schermo nero con audio muto invece di interrompere lo stream. È l'unico caso in cui il programma codifica video, e consuma pochissimo.

**Riallineamento giornaliero.** Senza ricodifica i tempi del flusso, dopo circa 26 ore, si "riavvolgerebbero". Per evitarlo, una volta al giorno il collegamento RTMP viene riaperto, alla fine del primo video dopo l'ora scelta (di serie le 03:00). Il passaggio dura un paio di secondi, tra un video e l'altro. L'ora si cambia in Impostazioni, Trasmissione.

**Copie di sicurezza.** Palinsesti, impostazioni e utenti vengono copiati sullo Storage di Bunny pochi minuti dopo ogni modifica e comunque una volta al giorno. Il file si chiama, per esempio, `2026.11.13 Backup regia.json`. Da Impostazioni si può ripristinare una copia o scaricarne una sul computer.

**Storico.** Lo storico mostra le ultime tre giornate. I dati di messa in onda più vecchi di 10 giorni e i palinsesti più vecchi di 60 giorni vengono cancellati da soli.

## Installazione su Bunny Magic Containers

Servono un account GitHub, che pubblica l'immagine del container, e l'account Bunny dell'emittente.

1. **Mettere il codice su GitHub.** Creare un repository, anche privato, e caricarci tutti i file di questa cartella, compresa `.github`. A ogni caricamento sul ramo `main` GitHub costruisce l'immagine e la pubblica come `ghcr.io/NOME-UTENTE/NOME-REPOSITORY:latest`; l'avanzamento si vede nella scheda Actions. Se il repository è privato, il pacchetto dell'immagine resta privato. In tal caso le strade sono due: renderlo pubblico (in GitHub: Packages, il pacchetto, Package settings, Change visibility) oppure collegare il registro privato in Bunny (Magic Containers, Image Registries) con un token GitHub con permesso `read:packages`.

2. **Creare l'applicazione in Bunny.** Andare in Magic Containers, Add App, e scegliere l'immagine appena pubblicata con il tag `latest`. Poi:
   - **una sola regione e una sola istanza**, con autoscaling minimo 1 e massimo 1, altrimenti partirebbero due trasmissioni;
   - **variabile d'ambiente** `ADMIN_PASSWORD` con la password del primo accesso (utente `admin`);
   - **volume persistente** montato su `/data`, da 1 GB: lì stanno palinsesti, impostazioni e utenti;
   - **endpoint di tipo CDN** sulla porta `8080`.

   Fatto questo, si avvia il deploy.

3. **Primo accesso.** Aprire l'indirizzo dell'endpoint mostrato da Bunny (oppure un dominio proprio, da Endpoints) ed entrare con `admin` e la password scelta. Conviene subito creare gli utenti personali in Utenti e cambiare la password di `admin`. Se `ADMIN_PASSWORD` non è stata impostata, il programma crea una password casuale e la scrive nei log del container.

4. **Impostazioni.** Nella pagina Impostazioni vanno inseriti:
   - indirizzo RTMP e chiave dello stream;
   - indirizzo del video di riempimento;
   - nome e password della Storage Zone per le copie di sicurezza. La password si trova in Bunny, Storage, la zona, FTP & API Access.

   Da quel momento la trasmissione parte.

Se le pagine sembrano non aggiornarsi, controllare che la pull zone dell'endpoint rispetti le intestazioni di cache del programma: le pagine sono inviate con `no-store` e i file statici hanno la versione nel percorso.

## Aggiornamenti

Ogni modifica caricata su GitHub produce una nuova immagine. In Bunny basta ridistribuire l'app con il tag `latest`, oppure attivare l'aggiornamento automatico descritto in fondo a `.github/workflows/immagine.yml`. I dati sul volume `/data` restano. Il riavvio interrompe la trasmissione per qualche secondo; alla ripartenza il motore si riallinea all'orario.

## Prova sul proprio computer

Con Docker installato:

```
docker build -t regia .
docker run --rm -p 8080:8080 -e ADMIN_PASSWORD=provaprova -e COOKIE_SECURE=0 -v "$PWD/data:/data" regia
```

Poi aprire http://localhost:8080 ed entrare con `admin` e `provaprova`. `COOKIE_SECURE=0` serve solo in prova, senza https.

## Variabili d'ambiente

| Variabile | Uso | Valore di serie |
|---|---|---|
| `ADMIN_PASSWORD` | password del primo utente, solo al primo avvio | casuale, scritta nei log |
| `ADMIN_USER` | nome del primo utente | `admin` |
| `PORT` | porta della pagina web | `8080` |
| `DATA_DIR` | cartella dei dati (volume persistente) | `/data` |
| `TZ_NAME` | fuso orario del palinsesto | `Europe/Rome` |
| `COOKIE_SECURE` | `0` solo per prove senza https | `1` |

## Struttura

| File | Contenuto |
|---|---|
| `app/engine.py` | motore di messa in onda |
| `app/schedule.py` | calcolo degli orari |
| `app/media.py` | controllo dei video con ffprobe |
| `app/backup.py` | copie su Bunny Storage |
| `app/web.py` | pagina di gestione |
| `app/templates`, `app/static` | aspetto della pagina |
| `Dockerfile`, `entrypoint.sh` | container |
| `.github/workflows/immagine.yml` | pubblicazione dell'immagine |

## Problemi frequenti

**"Manca l'indirizzo RTMP".** Va inserito in Impostazioni, Trasmissione.

**Stato "Errore" con un dettaglio sul collegamento.** Il server RTMP rifiuta o chiude il collegamento. Vanno controllati indirizzo e chiave, e che il server accetti lo stream. Il programma intanto riprova da solo.

**Un video viene segnalato "Non conforme".** Il motivo è scritto sotto il titolo, per esempio un bitrate troppo alto o una risoluzione diversa. Va riesportato con il preset giusto e ricontrollato con il pulsante a forma di freccia circolare.

**"Il motore di messa in onda non risponde".** Il motore riparte da solo entro pochi secondi. Se il messaggio resta per più di un minuto, riavviare l'app da Bunny e guardare i log del container.
