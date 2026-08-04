# Roadmap: Cronologia conversazioni persistente per workspace

Stato: **intervento futuro separato**. Questo documento definisce il contratto
tecnico e l'ordine di implementazione; nessun codice della cronologia è stato
ancora implementato.

## Contesto

La memoria conversazionale attuale non è uno storico:

- `ConversationMemoryStore` è in-process; l'alternativa Redis usa comunque un
  TTL.
- La memoria dura 6 ore, conserva al massimo 100 conversazioni e riassume i
  turni vecchi mantenendo solo gli ultimi quattro.
- Il browser conserva soltanto il `conversation_id` in `sessionStorage`; il
  transcript visibile vive nel DOM.
- Gli ID pubblici vengono trasformati in chiavi interne diverse:
  `{workspace}:{id}`, `{workspace}:kb:{kb_id}:{id}` e
  `{workspace}:multi-chat:{id}`.
- Gli endpoint `DELETE /conversation/<conversation_id>` e
  `DELETE /api/v1/conversations/<conversation_id>` cancellano la memoria
  temporanea, non uno storico persistente.

## Obiettivi

- Salvare in modo durevole conversazioni, configurazione, messaggi e fonti
  sicure.
- Mantenere `ConversationMemoryStore` come cache calda per prompt, retrieval e
  summary.
- Elencare, aprire, rinominare, archiviare ed eliminare le conversazioni.
- Riprendere una conversazione anche dopo restart o scadenza della cache.
- Fare in modo che `New Chat` non cancelli la conversazione precedente quando
  tutti i turni visibili risultano salvati.
- Garantire isolamento per workspace e autorizzazione completa delle Knowledge
  Base per le API key.
- Definire retention, quota, backup e cancellazione senza perdita silenziosa.

## Non-obiettivi

- Nessun backfill fedele delle conversazioni precedenti al rilascio.
- Nessun ORM: lo store usa `sqlite3` della standard library.
- Nessuna modifica incompatibile agli endpoint v1 esistenti.
- Nessun salvataggio di prompt risolti, segreti, path locali o contesto RAG
  grezzo.

## Decisioni fissate

| Decisione | Scelta |
|---|---|
| Storage | SQLite separato per workspace |
| Memoria RAG | Resta temporanea e viene idratata dallo storico quando necessario |
| Continuità live | La memoria resta invariata con history disabilitata/non richiesta; con history richiesta riceve soltanto il risultato che vince il commit |
| New Chat | Ruota l'ID senza DELETE; se la conversazione non è salvata richiede conferma distruttiva |
| Fallimento storico | La risposta completa resta visibile come draft con `history_saved=false`, ma non entra nel contesto finché non viene salvata o trasferita esplicitamente a una nuova chat volatile |
| Continuità | Ogni turno persistibile ha `turn_id` e `parent_turn_id`; il server accetta soltanto una catena lineare |
| Titolo | Primo messaggio user normalizzato e troncato a 80 caratteri, rinominabile fino a 120 |
| Eliminazione KB | Hard-delete delle conversazioni collegate, incluse quelle multi-KB, con conteggio preventivo e conferma |
| API esistenti | Il DELETE v1 continua a significare “clear memoria temporanea” |
| Persistenza API query | Opt-in con `persist_history=true`; nessuna nuova conservazione silenziosa per i client esistenti |
| API history esterna | Scope dedicati `history_read` e `history_manage`; nessun accesso implicito con il solo `query` |
| Migrazione | Nessun backfill; lo storico parte dai nuovi turni post-rilascio |

## Architettura

### Componenti

1. **ConversationMemoryStore**
   Continua a gestire il contesto caldo, il summary rolling e il TTL.

2. **ConversationHistoryStore**
   Gestisce esclusivamente SQLite, transazioni, migrazioni, paginazione,
   retention e cancellazione.

3. **ConversationService**
   Nuovo livello applicativo che coordina store durevole e memoria temporanea.
   Riceve sempre:

   - workspace corrente;
   - `client_conversation_id` non trasformato;
   - `scope_key` interno già calcolato;
   - modalità `default|kb|multi`;
   - Agent, provider, modello, prompt reference, lingua e Knowledge Base;
   - messaggio user, risposta completa e fonti già sanificate.

4. **PendingTurnResultStore**
   Buffer temporaneo e limitato, sullo stesso backend in-process/Redis della
   memoria, che conserva il payload completo e sanificato di un turno `ready`
   finché SQLite non lo promuove a `complete`. Permette un retry senza una
   seconda chiamata al provider; non è uno storico durevole.

Il servizio evita di inserire la persistenza direttamente in
`rag_engine._append_conversation_turn`: in quel punto non sono disponibili in
modo affidabile ID pubblico, workspace, Agent e fonti complete.

Il feature flag governa solo lo storico. Con
`RAG_CONVERSATION_HISTORY_ENABLED=0`, il servizio mantiene esattamente il
comportamento corrente della memoria temporanea e non apre SQLite.

### File e connessioni

- Database: `<WORKSPACE_DATA_DIR>/<workspace_id>/conversations.db`.
- Creazione lazy tramite
  `conversation_history_store(workspace, app=None)` in `workspace.py`.
- Ogni operazione apre una connessione breve con:

  - `PRAGMA journal_mode=WAL`;
  - `PRAGMA foreign_keys=ON`;
  - `PRAGMA busy_timeout=5000`;
  - `PRAGMA secure_delete=ON`;
  - permessi file `0600`.

- Migrazioni versionate con `PRAGMA user_version`; non basta
  `CREATE TABLE IF NOT EXISTS`.
- Scritture concorrenti eseguite con `BEGIN IMMEDIATE`.

## Identità della conversazione

Il solo ID scelto dal client non è sufficiente: lo stesso valore può essere
usato con scope default, KB singola o multi-KB e produrre memorie interne
diverse.

Ogni record persistente contiene quindi:

- `id`: UUID server-side usato dagli endpoint di history;
- `client_conversation_id`: ID validato ricevuto dal client;
- `scope_key`: chiave interna completa e univoca usata dalla memoria RAG;
- `scope_kind`: `default|kb|multi`.

La UI apre lo storico tramite `id`, ma quando riprende una chat ripristina
`client_conversation_id` e la configurazione necessaria a ricostruire lo
stesso `scope_key`.

## Flusso di scrittura

La persistenza viene orchestrata nel livello applicativo, dove sono disponibili
payload, configurazione e risultato finale.

### Query non streaming

1. Validare workspace, Agent, prompt e Knowledge Base.
2. Calcolare `client_conversation_id` e `scope_key`.
3. Chiamare `ConversationService.begin_turn(...)` con
   `turn_id`, `parent_turn_id` e fingerprint della richiesta.
4. Se il turno è già completo e il fingerprint coincide, restituire la
   risposta salvata con `replayed=true` senza richiamare retrieval o modello.
   Se lo stesso ID ha un fingerprint diverso, restituire `409
   turn_id_conflict`.
5. Se il turno è nuovo, prenotarlo con lease, idratare la memoria ed eseguire
   la query RAG senza modificarla direttamente.
6. Se la reservation non può essere acquisita prima della generazione,
   restituire un errore esplicito. La UI può offrire “Continua senza salvare”,
   che crea un nuovo `client_conversation_id` volatile prima di riprovare; non
   si degrada silenziosamente la stessa conversazione.
7. Serializzare solo le fonti sicure, salvare il risultato nel
   `PendingTurnResultStore`, marcare la reservation `ready` e chiamare
   `ConversationService.complete_turn(...)`.
8. Soltanto il payload restituito dal commit vincente viene aggiunto alla
   memoria con `append_turn_once`. Se stage o commit falliscono, mostrare la
   risposta come draft non salvata e bloccare il follow-up finché l'utente non
   ritenta o passa esplicitamente a una nuova chat volatile.
9. Restituire la risposta con
   `history_status: disabled|not_requested|saved|error|client_turn_id_required` e
   `history_saved: true|false`.

La prenotazione impedisce che due retry concorrenti con lo stesso `turn_id`
generino risposte diverse. Un secondo processo vede `generating` e riceve
`409 turn_in_progress` con `Retry-After`; una lease scaduta può essere acquisita
di nuovo solo con lo stesso fingerprint. La lease viene rinnovata durante una
generazione lunga.

Se una reservation è `ready`, il retry usa esclusivamente il payload pending
con lo stesso fingerprint. Se quel buffer è scaduto dopo TTL/restart, ritorna
`409 volatile_result_lost`: non rigenera silenziosamente una risposta diversa.
La UI può allora offrire “Rigenera e sostituisci il draft” come azione esplicita.

### Query streaming e Code Interpreter

- Il wrapper applicativo intercetta l'evento finale `done`.
- Prima di inviare `done` al client, salva atomicamente il turno tramite
  `ConversationService`.
- Aggiunge `history_saved` e l'eventuale `history_id` all'evento.
- Se lo stream viene interrotto prima del completamento, il turno incompleto non
  entra nello storico iniziale.
- La persistenza streaming richiede NDJSON, perché soltanto l'evento `done` può
  confermare o riconciliare il risultato. Il formato testuale legacy di
  `query_rag_stream` resta invariato, continua a usare soltanto la memoria
  temporanea e rifiuta `persist_history=true` prima di iniziare lo stream,
  indicando al client di negoziare NDJSON.
- Un retry NDJSON di un turno già completo riproduce risposta e fonti salvate
  senza richiamare il provider e aggiunge `replayed=true`.
- Se il worker perde la lease dopo aver emesso token, non aggiunge il proprio
  draft alla memoria. NDJSON sostituisce il draft con il risultato durevole del
  worker vincente nell'evento finale, oppure emette un errore.
- Code Interpreter usa lo stesso servizio e salva un `message_type` e
  metadati sicuri, non path del container o file locali.

### Ordine nel ConversationService

1. Se history è abilitata, `begin_turn` crea o acquisisce una reservation
   `generating`. Verifica transazionalmente fingerprint, lease e che
   `parent_turn_id` coincida con l'ultimo turno completo. La prima richiesta usa
   `parent_turn_id=null`.
2. Terminata la generazione, `stage_turn_result` salva il payload nel buffer e
   porta la reservation a `ready` soltanto se la lease appartiene ancora al
   worker.
3. `complete_turn` verifica nuovamente il proprietario, inserisce user +
   assistant, aggiorna la reservation a `complete` e avanza
   `conversations.last_turn_id` in una sola transazione. Restituisce le
   rispettive `sequence`.
4. Dopo il commit, il servizio rilegge il turno `complete` e usa quel payload
   autorevole per `ConversationMemoryStore.append_turn_once`. Un worker che ha
   perso la lease non esegue mai l'append; un retry non duplica il contesto.
5. Se stage o commit falliscono, il draft non entra nella memoria della
   conversazione persistente. Il parent durevole rimane invariato, quindi non
   si possono creare buchi dopo TTL, restart o cambio worker.
6. Se viene prodotto un nuovo summary e lo storico è completo, lo store
   persistente lo aggiorna con compare-and-swap su `summary_version` e registra
   fino a quale `sequence` è valido.
7. Con history disabilitata, non richiesta oppure con una nuova chat dichiarata
   volatile prima della generazione, SQLite viene saltato e la memoria mantiene
   il comportamento corrente.

`history_saved` indica che l'intera conversazione visibile, non soltanto
l'ultimo turno, è persistita. Il browser mantiene l'AND degli esiti ricevuti
per la sessione. La fase iniziale non tenta di ricostruire automaticamente un
turno il cui buffer pending è perso: serve un retry identico finché il buffer
esiste, oppure una rigenerazione/sostituzione esplicita.

Il client genera un `turn_id` per ogni invio, lo riusa nei retry e invia come
`parent_turn_id` l'ID del turno visibile precedente. Entrambi sono validati e
hanno una lunghezza massima definita dal contratto. Quando history è abilitata,
`turn_id` è obbligatorio per la persistenza: i client legacy che non lo inviano
continuano a ricevere risposta e memoria temporanea, ma ottengono
`history_status=client_turn_id_required` e `history_saved=false`.

Il `request_fingerprint` è SHA-256 di un JSON canonico che copre tutti gli
input effettivi della generazione: domanda, parent, Agent, provider/modello,
temperature, `k`, prompt reference, lingua, selezione KB, `client_context`,
modalità Code Interpreter e allegati. Per file o contenuti sensibili usa ID
stabili e digest del contenuto autorizzato, mai path locali, token o segreti.
Chiavi ordinate, numeri normalizzati e distinzione esplicita fra valore assente
e default rendono il digest riproducibile. Un qualsiasi input rilevante diverso
produce `409 turn_id_conflict` invece di un replay non pertinente.

## Modello dati SQLite

```sql
CREATE TABLE conversations (
  id                       TEXT PRIMARY KEY,
  client_conversation_id   TEXT NOT NULL,
  scope_key                TEXT NOT NULL UNIQUE,
  scope_kind               TEXT NOT NULL
                             CHECK (scope_kind IN ('default', 'kb', 'multi')),
  title                    TEXT NOT NULL DEFAULT '',
  status                   TEXT NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'archived')),
  agent_id                 TEXT NOT NULL DEFAULT '',
  agent_name               TEXT NOT NULL DEFAULT '',
  provider_id              TEXT NOT NULL DEFAULT '',
  model_id                 TEXT NOT NULL DEFAULT '',
  prompt_ref               TEXT NOT NULL DEFAULT '{}',
  response_language        TEXT NOT NULL DEFAULT 'auto',
  summary                  TEXT NOT NULL DEFAULT '',
  summary_version          INTEGER NOT NULL DEFAULT 0,
  summary_through_sequence INTEGER NOT NULL DEFAULT 0,
  message_count            INTEGER NOT NULL DEFAULT 0,
  payload_bytes            INTEGER NOT NULL DEFAULT 0,
  last_turn_id             TEXT,
  created_at               REAL NOT NULL,
  updated_at               REAL NOT NULL,
  archived_at              REAL
);

CREATE INDEX idx_conversations_updated
  ON conversations(updated_at DESC);
CREATE INDEX idx_conversations_status_updated
  ON conversations(status, updated_at DESC);

CREATE TABLE conversation_knowledge_bases (
  conversation_id          TEXT NOT NULL,
  knowledge_base_id        TEXT NOT NULL,
  is_selected              INTEGER NOT NULL DEFAULT 1
                             CHECK (is_selected IN (0, 1)),
  first_used_at            REAL NOT NULL,
  last_used_at             REAL NOT NULL,
  PRIMARY KEY (conversation_id, knowledge_base_id),
  FOREIGN KEY (conversation_id)
    REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX idx_conversation_kb
  ON conversation_knowledge_bases(knowledge_base_id, conversation_id);

CREATE TABLE turn_requests (
  conversation_id          TEXT NOT NULL,
  turn_id                  TEXT NOT NULL,
  parent_turn_id           TEXT,
  request_fingerprint      TEXT NOT NULL,
  status                   TEXT NOT NULL
                             CHECK (status IN
                               ('generating', 'ready', 'complete', 'failed')),
  result_digest            TEXT,
  lease_token              TEXT,
  lease_expires_at         REAL,
  created_at               REAL NOT NULL,
  updated_at               REAL NOT NULL,
  PRIMARY KEY (conversation_id, turn_id),
  FOREIGN KEY (conversation_id)
    REFERENCES conversations(id) ON DELETE CASCADE,
  FOREIGN KEY (conversation_id, parent_turn_id)
    REFERENCES turn_requests(conversation_id, turn_id)
);
CREATE UNIQUE INDEX idx_turn_requests_linear_parent
  ON turn_requests(conversation_id, COALESCE(parent_turn_id, ''))
  WHERE status IN ('generating', 'ready', 'complete');

CREATE TABLE messages (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id          TEXT NOT NULL,
  turn_id                  TEXT NOT NULL,
  role                     TEXT NOT NULL
                             CHECK (role IN ('user', 'assistant')),
  message_type             TEXT NOT NULL DEFAULT 'text',
  content                  TEXT NOT NULL,
  sequence                 INTEGER NOT NULL,
  sources                  TEXT NOT NULL DEFAULT '[]',
  metadata                 TEXT NOT NULL DEFAULT '{}',
  payload_bytes            INTEGER NOT NULL DEFAULT 0,
  created_at               REAL NOT NULL,
  FOREIGN KEY (conversation_id)
    REFERENCES conversations(id) ON DELETE CASCADE,
  FOREIGN KEY (conversation_id, turn_id)
    REFERENCES turn_requests(conversation_id, turn_id) ON DELETE CASCADE,
  UNIQUE (conversation_id, sequence),
  UNIQUE (conversation_id, turn_id, role)
);
CREATE INDEX idx_messages_conversation_sequence
  ON messages(conversation_id, sequence);
```

`prompt_ref`, `sources` e `metadata` sono JSON validati e limitati. Le
Knowledge Base sono normalizzate nella tabella di relazione: non si usa un
array JSON per autorizzazione o cancellazione. Le righe rappresentano l'unione
monotona di tutte le KB usate nella conversazione e non vengono rimosse quando
cambia la selezione; `is_selected` identifica invece l'insieme corrente da
ripristinare al resume. Autorizzazione, conteggio e delete KB usano sempre
l'unione completa, mai soltanto le righe selezionate.

`payload_bytes` conta i byte UTF-8 logici di content, sources e metadata ed è
aggiornato nella stessa transazione degli insert. Il contatore sulla
conversazione rende applicabili le quote senza rieseguire una scansione di
tutti i messaggi a ogni turno; una verifica periodica riconcilia i contatori.

## ConversationHistoryStore

Nuovo modulo `app/utils/conversation_history_store.py`:

```text
ensure_schema() -> None
begin_turn(
    *,
    client_conversation_id,
    scope_key,
    scope_kind,
    turn_id,
    parent_turn_id,
    request_fingerprint,
    lease_token,
    selected_knowledge_base_ids,
    ... current configuration ...,
) -> new | generating | ready | replay | conflict | continuity_error
mark_turn_ready(
    scope_key,
    turn_id,
    lease_token,
    result_digest,
) -> bool
complete_turn(
    *,
    scope_key,
    turn_id,
    lease_token,
    request_fingerprint,
    user_content,
    assistant_content,
    message_type,
    selected_knowledge_base_ids,
    agent_id,
    agent_name,
    provider_id,
    model_id,
    prompt_ref,
    response_language,
    sources,
    metadata,
) -> dict
    # BEGIN IMMEDIATE
    # validate reservation, owner, fingerprint and parent
    # insert user + assistant atomically
    # complete reservation and advance last_turn_id
    # update title, message_count, payload_bytes and updated_at
renew_turn_lease(scope_key, turn_id, lease_token) -> bool
fail_turn(scope_key, turn_id, lease_token) -> bool
get_turn(scope_key, turn_id) -> dict | None
get(history_id) -> dict | None
get_by_scope_key(scope_key) -> dict | None
list_messages(
    history_id,
    *,
    before_sequence=None,
    limit=50,
) -> (list[dict], next_cursor)
list(*, page, per_page, status=None) -> (list[dict], pagination)
rename(history_id, title) -> dict
archive(history_id) -> dict
unarchive(history_id) -> dict
update_summary(
    scope_key,
    summary,
    *,
    expected_version,
    through_sequence,
) -> bool
delete(history_id) -> bool
delete_by_knowledge_base(knowledge_base_id) -> int
count_by_knowledge_base(knowledge_base_id) -> int
quota_status() -> dict
```

La sequenza viene calcolata e inserita nella stessa transazione. Un retry con
lo stesso `turn_id` viene riconosciuto prima della generazione e restituisce il
record esistente senza duplicare messaggi, consumo provider, memoria o
`message_count`. Una reservation `failed` può essere riaperta soltanto dallo
stesso `turn_id` con fingerprint invariato; un nuovo turno deve discendere
dall'ultimo turno `complete`.

`begin_turn` può creare uno stub di conversazione per la foreign key della
reservation. List e retention ignorano gli stub con `message_count=0`; una
reservation fallita o scaduta viene mantenuta per il periodo diagnostico
definito e poi ripulita; il cleanup elimina anche gli stub rimasti senza
reservation. Union KB, selezione corrente e configurazione diventano visibili
soltanto in `complete_turn`, usando lo snapshot congelato con cui è stato
calcolato il fingerprint.

Il pending store usa una chiave che include workspace, `scope_key`, `turn_id` e
`lease_token`. `mark_turn_ready` registra in SQLite soltanto il digest del
payload. `complete_turn` accetta il contenuto dal buffer solo se ownership e
digest coincidono, e il buffer viene cancellato soltanto dopo il commit.

L'aggiornamento del summary usa una clausola CAS su `summary_version` e accetta
solo un `through_sequence` crescente. Un job lento non può sovrascrivere un
summary più recente. `ConversationTurn` conserva `turn_id` e l'eventuale
`assistant_sequence`; un summary viene persistito solo se tutti i turni che
copre hanno una sequence durevole.

## Idratazione della memoria

L'idratazione è server-side e lazy; non dipende dal click nel drawer.

Per ogni query con `scope_key`:

1. Validare prima tutte le Knowledge Base richieste.
2. Leggere lo snapshot dalla memoria temporanea.
3. Se lo snapshot è vuoto, cercare la conversazione per `scope_key`.
4. Verificare nuovamente che tutte le KB persistenti siano attive e autorizzate.
5. Caricare il `summary` e i messaggi con
   `sequence > summary_through_sequence`; se il cursore è incoerente, ignorare
   il summary e rigenerarlo in modo controllato. Il fallback legge a pagine,
   sintetizza i messaggi vecchi e mantiene in memoria soltanto gli ultimi
   `CONVERSATION_RECENT_TURNS_TO_KEEP` turni: non carica mai un transcript
   illimitato.
6. Chiamare `ConversationMemoryStore.hydrate_if_absent` con un'operazione
   atomica per backend: `RLock` in-process e Lua o `WATCH/MULTI` su Redis.
7. Leggere un nuovo snapshot. Se un'altra richiesta aveva già creato lo stato,
   usare quello senza sovrascriverlo.
8. Proseguire con retrieval e generazione.

Questo flusso vale anche per client API che riusano un ID dopo TTL o restart,
senza passare dalla UI.

## API

### Sessione web

| Metodo | Path | Scopo |
|---|---|---|
| GET | `/api/conversations` | Elenco paginato |
| GET | `/api/conversations/<history_id>` | Record e configurazione |
| GET | `/api/conversations/<history_id>/messages` | Messaggi a cursore, `limit` massimo 200 |
| PATCH | `/api/conversations/<history_id>` | Rename o archive/unarchive |
| DELETE | `/api/conversations/<history_id>` | Hard-delete esplicito e clear della cache collegata |

Tutte le route usano `@require_login` e ricavano il workspace dalla sessione.
Un ID appartenente a un altro workspace restituisce `404`.

List e get espongono soltanto turni `complete`. Possono includere
`has_incomplete_turn=true` quando esiste una reservation pending/failed, così
il drawer non presenta come completa una conversazione di cui è noto un
salvataggio interrotto.

### Compatibilità API

Gli endpoint esistenti mantengono il significato attuale:

- `DELETE /conversation/<client_conversation_id>`: clear memoria sessione;
- `DELETE /api/v1/conversations/<client_conversation_id>`: clear memoria API.

I payload query accettano in modo additivo `persist_history`, `turn_id` e
`parent_turn_id`. Per la sessione web, la persistenza è attiva quando lo è il
feature flag. Per le API key è invece opt-in con `persist_history=true`, così il
rilascio non introduce conservazione inattesa per i client esistenti. I client
che fanno opt-in devono inviare `turn_id` fin dal primo turno e conservare
l'ultimo ID completo/visibile; senza opt-in la query mantiene il comportamento
attuale e ritorna `history_status=not_requested`. Per lo streaming, l'opt-in è
valido soltanto sul formato NDJSON; il raw legacy non cambia contratto.

L'esposizione dello storico alle API esterne è una fase separata:

| Metodo | Path | Auth |
|---|---|---|
| GET | `/api/v1/conversation-history` | `@require_api_scope("history_read")` |
| GET | `/api/v1/conversation-history/<history_id>` | `@require_api_scope("history_read")` |
| GET | `/api/v1/conversation-history/<history_id>/messages` | `@require_api_scope("history_read")` |
| PATCH | `/api/v1/conversation-history/<history_id>` | `@require_api_scope("history_manage")` |
| DELETE | `/api/v1/conversation-history/<history_id>` | `@require_api_scope("history_manage")` |

Per una API key, una conversazione è accessibile solo se l'insieme completo
delle sue Knowledge Base è un sottoinsieme della allowlist della chiave.
Nessuna intersezione parziale è ammessa. Le violazioni restituiscono `404`.
I nuovi scope non vengono assegnati automaticamente alle API key esistenti;
anche le chiavi history devono avere una allowlist KB non vuota.

La paginazione viene eseguita in SQL con `COUNT(*)`, `LIMIT` e `OFFSET`;
l'envelope riusa la shape di `paginate_items` senza caricare tutti i record in
memoria. I messaggi usano invece un cursore su `sequence`: default 50, massimo
200, query in ordine discendente e risposta riordinata cronologicamente.

## UI: Previous chats

- Drawer laterale con ricerca, paginazione e stati loading/empty/error.
- Con il feature flag disabilitato, drawer e copy “salvata” non vengono mostrati
  e resta il comportamento di avviso attuale.
- Click su una voce:

  1. carica record e l'ultima pagina del transcript tramite `history_id`;
  2. rivalida Agent, modello, prompt e Knowledge Base;
  3. ripristina `client_conversation_id` in `sessionStorage`;
  4. ripristina `last_turn_id`, usato come parent del prossimo invio;
  5. renderizza nuovamente i messaggi usando lo stesso sanitizzatore Markdown;
  6. mostra warning e fallback a `None` se l'Agent non è più disponibile.

- I messaggi precedenti vengono caricati progressivamente quando l'utente
  risale il transcript; nessuna route o operazione DOM materializza l'intera
  conversazione senza limite.

- Non si passa automaticamente a `localStorage`: `sessionStorage` evita il
  resume implicito su dispositivi condivisi. Il drawer resta la source of truth
  per le conversazioni precedenti.
- Al reload della stessa tab, se `sessionStorage` contiene un ID attivo, la UI
  tenta il resume; su `404` pulisce l'ID.
- `New Chat` crea un nuovo ID senza inviare DELETE per la conversazione
  precedente.
- Se `history_saved=true`, la modale può dire che la conversazione resterà
  disponibile in Previous chats.
- Se history è disabilitata o anche un solo turno ha `history_saved=false`,
  resta l'avviso distruttivo attuale finché la conversazione non viene
  esportata o abbandonata esplicitamente.
- Un draft non salvato disabilita il campo di invio e offre tre azioni:

  1. ritentare lo stesso turno usando il pending result, senza provider;
  2. se il buffer è perso, rigenerare e sostituire esplicitamente il draft;
  3. continuare senza salvataggio in un nuovo ID volatile, importando nel nuovo
     contesto il transcript corrente solo dopo conferma.

La rigenerazione esplicita riacquisisce lo stesso `turn_id` e fingerprint con
un flag di sostituzione, quindi non crea un secondo figlio nella catena. Il
client elimina il draft precedente e usa soltanto il nuovo risultato.

## Sicurezza e privacy

- Il workspace deriva sempre da sessione o API key, mai dal payload.
- Le fonti vengono serializzate con una allowlist di campi; niente path locali,
  URL amministrativi non autorizzati, prompt risolti o segreti.
- Si persistono identificatori e label stabili delle fonti, non URL di download
  o token temporanei; eventuali link vengono rigenerati dopo l'autorizzazione.
- Markdown e metadati vengono sanificati nuovamente al rendering.
- File SQLite e snapshot di backup hanno permessi `0600`.
- Il pending result è namespaced per workspace, soggetto agli stessi limiti dei
  messaggi, non viene loggato e segue TTL e protezioni del backend Redis.
- Il database contiene dati sensibili in chiaro: deployment e documentazione
  devono esplicitare protezione del volume e cifratura dei backup.
- Limiti di input e quota vengono applicati prima della transazione.
- Agent, prompt e KB vengono rivalidati a ogni resume; non si considera
  affidabile la configurazione storica.

## Retention e quota

Nuove configurazioni:

```text
RAG_CONVERSATION_HISTORY_ENABLED=0
RAG_MAX_CONVERSATIONS_PER_WORKSPACE=200
RAG_CONVERSATION_HISTORY_RETENTION_DAYS=90
RAG_MAX_CONVERSATION_HISTORY_MESSAGE_CHARS=50000
RAG_MAX_CONVERSATION_MESSAGES=2000
RAG_MAX_CONVERSATION_BYTES=33554432
RAG_MAX_CONVERSATION_SOURCES_BYTES_PER_TURN=262144
RAG_MAX_CONVERSATION_METADATA_BYTES_PER_TURN=65536
RAG_MAX_CONVERSATION_HISTORY_BYTES=268435456
RAG_MAX_PENDING_HISTORY_TURNS_PER_WORKSPACE=100
RAG_CONVERSATION_TURN_LEASE_SECONDS=900
RAG_PENDING_TURN_RESULT_TTL_SECONDS=21600
RAG_INCOMPLETE_HISTORY_TURN_RETENTION_DAYS=7
```

Regole:

- I messaggi che superano il limite non vengono troncati silenziosamente: il
  commit fallisce con `history_saved=false`.
- Quota conversazione e workspace sono calcolate sui byte logici di content,
  sources e metadata, non soltanto sulla dimensione momentanea del file SQLite.
- Messaggi, sources e metadata hanno limiti indipendenti applicati prima della
  transazione.
- Le reservation incomplete hanno quota separata, lease rinnovabile e cleanup.
  Il result buffer scade dopo sei ore; le righe `ready/failed` conservano
  soltanto digest, fingerprint e metadati tecnici per sette giorni, poi vengono
  eliminate.
- Le conversazioni archiviate sono esenti dalla retention temporale ma contano
  sempre nella quota di spazio.
- Prima di rifiutare un nuovo commit, lo store elimina soltanto conversazioni
  non archiviate già oltre la retention.
- Se count o quota restano esauriti, il salvataggio viene rifiutato e la UI
  chiede all'utente di liberare spazio; nessuna conversazione valida viene
  eliminata silenziosamente.
- Cleanup e quota espongono metriche e log strutturati.

## Cancellazione

### Conversazione

`DELETE /api/conversations/<history_id>` esegue in transazione un hard-delete
SQLite con cascade dei messaggi e poi pulisce la memoria temporanea associata.
Pulisce anche gli eventuali pending result associati allo scope.
Un errore lascia il record intatto e ritorna una risposta esplicita; non sono
necessari stati `deleting/delete_failed` per una singola transazione locale.

L'hard-delete rimuove il record dal database corrente, ma non promette la
cancellazione retroattiva dalle copie di backup. La policy backup deve avere
retention e delete espliciti; il database live usa `secure_delete` e manutiene
WAL/free pages con checkpoint e vacuum pianificati.

### Knowledge Base

La decisione prodotto resta hard-delete:

- prima della conferma, la UI mostra quante conversazioni saranno eliminate;
- le conversazioni multi-KB collegate alla KB vengono eliminate interamente;
- il delete avviene solo quando la cancellazione della KB è effettivamente
  completata e sotto il lifecycle write lock globale, perché una conversazione
  multi-KB può essere usata da query che non includono la KB eliminata;
- API key e job concorrenti non possono leggere uno stato parziale.

### Workspace

`remove_workspace_files` elimina già l'intera directory workspace. Non serve
eseguire `remove_all()` prima di `rmtree`; le connessioni devono essere brevi
e chiuse e l'intera operazione deve restare protetta dal lifecycle lock.

## Backup e restore

Non si copia direttamente il file WAL e non ci si affida soltanto a
`wal_checkpoint(TRUNCATE)`.

- Durante il backup, usare `sqlite3.Connection.backup()` per creare uno
  snapshot consistente in una directory temporanea.
- Includere lo snapshot, non il DB live, nell'archivio.
- Registrare schema version, numero conversazioni e numero messaggi nel
  manifest.
- In restore, verificare `PRAGMA integrity_check` e versione schema prima di
  sostituire il workspace.
- Il pending result store non entra nel backup. Dopo restore, lease e token
  vengono azzerati e le reservation `generating/ready` passano a `failed` con
  motivo `restore_lost_pending`; non viene mai rigenerato automaticamente un
  risultato.
- Testare backup mentre una seconda connessione sta scrivendo.

## Migrazione

Le conversazioni esistenti non sono migrabili fedelmente:

- parte del transcript vive solo nel DOM;
- i turni vecchi sono già stati riassunti e scartati;
- la memoria temporanea può essere già scaduta.

Il rollout parte quindi dai nuovi turni. Il feature flag consente di distribuire
schema e write path prima di mostrare Previous chats.

## Fasi di implementazione

| Fase | Scope | Deliverable |
|---|---|---|
| **0 — contratto** | Schema e semantica | Migrazioni `user_version`, identity `history_id/client_id/scope_key`, limiti, comportamento failure e compatibilità API |
| **1 — store e service** | Persistenza nascosta | `ConversationHistoryStore`, `ConversationService`, append atomico/idempotente, union/selezione KB, memoria indipendente, summary con cursore/CAS, feature flag |
| **2 — integrazione** | Query e lifecycle | Hook non-stream/NDJSON/Code Interpreter, raw legacy senza history, hydration atomica, delete KB/workspace, snapshot backup, failure metrics |
| **3 — API sessione** | Lettura e gestione | List/get/patch/delete, cursore messaggi, isolamento workspace, resume dopo TTL/restart |
| **4 — UX** | Previous chats | Drawer, transcript a cursore, resume, rename/archive/delete, copy dinamica, New Chat non distruttivo, gestione `history_saved=false` |
| **5 — governance/API** | Retention ed esterno | Quota, cleanup, metriche, scope `history_read/history_manage`, endpoint `/api/v1/conversation-history`, documentazione OpenAPI |

## File previsti

| Azione | Path |
|---|---|
| Add | `app/utils/conversation_history_store.py` |
| Add | `app/utils/conversation_service.py` |
| Add | `app/utils/pending_turn_result_store.py` |
| Add | `app/routes/conversations.py` |
| Add | `tests/test_conversation_history.py` |
| Add | `tests/test_conversations_api.py` |
| Mod | `app/app.py` per orchestrazione query, streaming e registrazione route |
| Mod | `app/utils/rag_engine.py` per rimuovere l'append implicito dal motore |
| Mod | `app/utils/workspace.py` per helper e lifecycle |
| Mod | `app/utils/conversation_memory.py` per `append_turn_once` e `hydrate_if_absent` |
| Mod | `app/utils/vector_store/backup_manager.py` per snapshot SQLite |
| Mod | `app/templates/index.html` e `app/static/script.js` per Previous chats |
| Mod | `integrations/wordpress/rag-client/` per inviare `persist_history`, `turn_id` e `parent_turn_id` solo quando il client abilita la history |
| Mod | `app/config.py` e `.env.example` per feature flag, retention e quota |
| Mod | `app/utils/settings_store.py`, `app/utils/user_store.py`, `app/utils/auth.py`, `app/routes/admin_accounts.py` e `app/templates/admin_api_keys.html` per i nuovi scope history |
| Mod | `docs/API.md` e `docs/openapi.yaml` per i nuovi contratti |

## Test plan

### Store

- Migrazioni incrementali e `ensure_schema` idempotente.
- Append atomico user+assistant.
- Retry con stesso `turn_id` senza duplicati.
- Reservation concorrente, rinnovo lease, takeover scaduto e fingerprint
  differente con `409`.
- Fingerprint canonico sensibile a retrieval params, `client_context`, Code
  Interpreter e digest degli allegati, ma privo di segreti/path.
- History disabilitata/non richiesta con comportamento della memoria invariato.
- Fallimento di stage/commit con draft escluso dalla memoria persistente.
- Retry con stesso `turn_id` senza doppio append nella memoria.
- Ordine sequence e `message_count` sotto concorrenza.
- Union KB monotona, selezione corrente, titolo, rename, archive/unarchive e
  hard-delete cascade.
- Quota per messaggio/workspace e retention senza cancellazione silenziosa.
- Limiti per singola conversazione, numero messaggi, sources e metadata.

### Service e query

- Round-trip non-stream, NDJSON e Code Interpreter con fonti sicure.
- Commit eseguito prima dell'evento `done`.
- Stream raw invariato e rifiuto pre-stream dell'opt-in history; NDJSON
  interrotto senza turno parziale.
- Fallimento SQLite con draft e risposta `history_saved=false`.
- Perdita lease: nessun append del worker sconfitto e uso del risultato
  autorevole del vincitore.
- Retry di reservation `ready` usando lo stesso pending result senza provider;
  buffer perso con `409 volatile_result_lost`.
- Follow-up bloccato sul draft e fork volatile soltanto dopo scelta esplicita.
- Parent non durevole dopo TTL, restart e cambio worker senza creazione di buchi.
- Passaggio feature flag disabled→enabled su una conversazione già iniziata.
- Commit riuscito ma risposta persa: retry restituito dallo store senza nuova
  chiamata al provider; payload diverso con lo stesso `turn_id` restituisce
  `409`.
- `turn_id` assente: compatibilità query preservata, memoria aggiornata e
  history esplicitamente non salvata.
- API query senza opt-in: nessuna scrittura history e
  `history_status=not_requested`.
- Aggiornamento summary con CAS, cursore e rifiuto di job obsoleto.
- Hydration dopo TTL, restart e backend Redis.
- Race fra hydration e append senza sovrascrittura dello stato più recente.
- Stesso client ID con scope default, KB e multi senza collisioni.

### API e sicurezza

- Isolamento fra due workspace.
- `404` per history ID estraneo.
- API key con tutte le KB autorizzate.
- Rifiuto quando anche una sola KB non è nella allowlist.
- Scope `history_read` per lettura e `history_manage` per mutazioni; scope
  `query` o `ingest` da soli sono insufficienti.
- Compatibilità invariata del DELETE v1 esistente.
- Paginazione SQL delle conversazioni, cursore messaggi con limite massimo,
  rename, archive e delete.

### Lifecycle e backup

- Delete KB con conteggio preventivo e rimozione selettiva.
- Delete multi-KB secondo la decisione hard-delete e lock lifecycle globale.
- Delete workspace senza connessioni SQLite pendenti.
- Backup consistente durante una scrittura concorrente.
- Restore con `integrity_check` e schema incompatibile.
- Restore con reservation incomplete e pending buffer volutamente assente.

### UI

- Drawer loading/empty/error e navigazione tastiera.
- Resume di transcript e configurazione.
- Caricamento progressivo delle pagine precedenti del transcript.
- Feature flag disabilitato senza drawer e con avviso distruttivo invariato.
- Sanitizzazione Markdown e sources al replay.
- New Chat non distruttivo quando `history_saved=true`.
- Avviso mantenuto quando `history_saved=false`.
- Agent, prompt o KB non più disponibili con fallback esplicito.
