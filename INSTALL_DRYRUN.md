# Dry-run test locale Chat Agents

1) Avvia server locale:
```bash
cd /Users/daniloercoli/opt/Test-Rag-v1/integrations/wordpress/rag-client

# Sostituisci con porta libera se 8080 occupata
php -S 127.0.0.1:8080 -t .
```

2) Accedi a WordPress di test all’admin:
- http://localhost:8000/wp-admin/
- user: admin
- pass: password

3) Vai a Settings → Raguardian:
- Inserisci Base URL RAGuardian (es. https://your-rag-host.com)
- Inserisci API Key con scope query (admin → users → API key)
- Salva.

4) Verifica: compare la sezione **Chat Agents**. Attiva toggle, seleziona un agent dal menu a tendina (popolato via API call), salva.

5) Widget frontend:
- Apri una pagina pubblica: il selettore agent appare se `enable_chat_agents_mode=1`.
- Seleziona agent → invia query → il payload contiene `agent_id`.

6) Playwright test (se abilitato):
```bash
cd integrations/wordpress/rag-client
npm install
npm test
```

7) Se vuoi simulare chiamate API verso un RAGuardian locale, puoi usare ngrok o esporre il server in remoto con `ec_rag_query` raggiungibile dall’admin settings.

## Debug
- Admin JavaScript: F12 → Console tabs per errori CRUD agents.
- API calls: /wp-admin/admin-ajax.php?action=ec_rag_list_agents

Nota: Alcune funzionalità JWT/TTL richiedono RAGuardian configurato correttamente; il plugin rimanda messaggi di errore in pannello admin.
