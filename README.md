# syncfolder

Scambio e sincronizzazione di una cartella tra 2 PC via HTTP, passando per il tunnel Cloudflare.
Solo Python standard sul server (3.8+), nessuna dipendenza. Sull'altro PC basta il browser.

```
PC B (browser)  ──HTTPS──>  Cloudflare  ──tunnel──>  PC A  localhost:8080
 cartella scelta                                       server + pagina web
 nella pagina                                          ~/Sync
```

## File

- `syncfolder.py`: il server (e un client da riga di comando opzionale)
- `web.html`: la pagina web servita dal server. Deve stare **nella stessa cartella** di `syncfolder.py`

## 1. PC A — server

```bash
python3 syncfolder.py server --dir ~/Sync --tunnel
```

All'avvio il server:

- genera un **token nuovo** (tenuto solo in memoria): i link degli avvii precedenti smettono di funzionare
- avvia `cloudflared tunnel --url http://localhost:8080`
- stampa il **link di accesso**, che contiene già il token:

```
[...] tunnel attivo: https://qualcosa-di-casuale.trycloudflare.com

  Link di accesso (contiene il token, condividilo solo con chi deve entrare):
  ==============================================================================
  https://qualcosa-di-casuale.trycloudflare.com/#token=...
  ==============================================================================
```

Passa questo link all'altro PC. Il token sta dopo il `#`, quindi non viene mai inviato a Cloudflare né al server.

- Serve `cloudflared` installato (`brew install cloudflared`).
- È un *quick tunnel*: anche l'indirizzo cambia a ogni avvio. Se cloudflared si chiude, il server lo riavvia e stampa il nuovo indirizzo (il token resta lo stesso).
- `--token ...` (o la variabile `SYNC_TOKEN`) usa un token fisso invece di generarne uno.
- Senza `--tunnel` il server ascolta solo su `127.0.0.1:8080`, stampa il link locale e il tunnel lo gestisci tu.

## 2. PC B — pagina web

Apri l'indirizzo del tunnel (es. `https://sync.tuodominio.com`) con **Chrome o Edge**:

1. apri il **link di accesso**: il token viene salvato in questo browser e tolto dalla barra degli indirizzi
   (in alternativa incollalo nel campo Token e premi **Entra**). Dopo un riavvio del server serve il link nuovo:
   la pagina lo segnala e mette in pausa la sincronizzazione automatica finché non lo apri
2. **Scegli cartella…**: il browser chiede il permesso di modificarla. La cartella viene ricordata; nelle visite successive il permesso viene richiesto di nuovo al primo clic
3. usa i pulsanti:
   - **⬇ Scarica**: copia dal server alla cartella i file nuovi o diversi
   - **⬆ Carica**: copia dalla cartella al server i file nuovi o diversi
   - **⇅ Sincronizza**: aggiorna nei due sensi, con cancellazioni e conflitti (vedi sotto)
   - **Specchio**: con Scarica/Carica elimina anche, sulla destinazione, i file che nella sorgente non ci sono (chiede conferma)
   - **Sincronizza automaticamente**: ripete la sincronizzazione ogni N secondi finché la pagina è aperta

**Safari e Firefox** non permettono a una pagina di scrivere in una cartella. Lì la pagina mostra solo
**Carica una cartella** (invia i file nuovi o modificati) e **Scarica tutto (.zip)**.

Nella cartella scelta la pagina crea `.syncstate.json` e `.synccache.json`: servono a riconoscere le
cancellazioni e a non ricalcolare l'impronta dei file a ogni giro. Non vengono mai inviati al server.

## Come funziona Sincronizza

Lo stato ricorda l'impronta (SHA-256) di ogni file all'ultima sincronizzazione, così si capisce chi ha cambiato cosa:

| Situazione | Azione |
|---|---|
| cambiato/nuovo solo su B | upload su A |
| cambiato/nuovo solo su A | download su B |
| cancellato su un lato | cancellato anche sull'altro |
| modificato su entrambi | vince il più recente; l'altra versione resta come `nome.conflict-<pc>-<data>.ext` su tutti e due |
| cancellato da una parte e modificato dall'altra | il file modificato viene tenuto |

- Gli upload vanno a pezzi da 32 MB, quindi il limite di 100 MB per richiesta di Cloudflare non è un problema.
- Ogni upload è verificato con SHA-256 e scritto in modo atomico, quindi un trasferimento interrotto non lascia file a metà.
- Ignorati: `.DS_Store`, `Thumbs.db`, `desktop.ini`, link simbolici, cartelle vuote.

## .gitignore

I file esclusi dai `.gitignore` non vengono sincronizzati. Valgono anche i `.gitignore` nelle sottocartelle, con le stesse regole di git:

- ogni `.gitignore` vale per la sua cartella e per tutte le sottocartelle
- `*.log`, `?`, `[abc]`, `**/`, `dir/**`
- `/build` vale solo nella cartella del `.gitignore`; `build` vale a qualsiasi profondità
- `node_modules/` (con la `/` finale) esclude solo cartelle
- `!keep.log` riammette un file; se è esclusa la cartella che lo contiene, resta escluso (come in git)
- i `.gitignore` più profondi hanno la precedenza su quelli sopra

I `.gitignore` stessi vengono sincronizzati. Se un file è escluso **da uno qualsiasi dei due lati**, non viene
né copiato né cancellato su nessuno dei due: la modalità Specchio non elimina mai file ignorati.
Se cambi un `.gitignore`, i file che prima erano esclusi e ora non lo sono più vengono sincronizzati alla prossima occasione.

## Sicurezza

- Tutte le operazioni sui file richiedono il token. La pagina in sé non contiene segreti.
- Lo zip si scarica con un link monouso valido 60 secondi, così il token non finisce mai in un URL.
- Per un livello in più puoi mettere davanti al dominio **Cloudflare Access** (login con email).

## Client da riga di comando (opzionale)

Se sul PC B preferisci uno script invece del browser (serve solo `syncfolder.py`):

```bash
python3 syncfolder.py client --dir ~/Sync --url https://sync.tuodominio.com --token TOKEN_DEL_SERVER --interval 10
```

`TOKEN_DEL_SERVER` è la parte dopo `#token=` del link di accesso (cambia a ogni avvio del server, a meno di usare `--token` anche sul server).

- `--once`: una sola sincronizzazione
- `--mode down` / `--mode up`: solo scarica / solo carica, una volta
- `--mirror`: con `--mode` elimina sulla destinazione i file che non ci sono nella sorgente

Usa lo stesso `.syncstate.json` della pagina, quindi i due modi si possono alternare sulla stessa cartella.
