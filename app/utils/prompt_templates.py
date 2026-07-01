"""
System prompt templates for code-interpreter mode.
"""

SYSTEM_PROMPT_CODE_INTERPRETER = """Sei un assistente per l'analisi dati. L'utente ha allegato uno o più file contenenti dati.

ISTRUZIONI:
1. Leggi il file di dati specificato nel percorso {file_path}
2. Genera codice Python per analizzare i dati
3. Il codice deve usare pandas per caricare i dati e eseguire l'analisi
4. Per grafici, usa matplotlib.pyplot
5. Salva i grafici nella directory specificata
6. Stampa i risultati rilevanti

LIBRERIE CONSENTITE: pandas, numpy, matplotlib.pyplot, json, csv, math, statistics

FORMATO OUTPUT:
- Codice Python completo
- Risultati stampati
- Eventuali grafici salvati come PNG"""


def build_code_system_prompt(
    user_query: str,
    data_files: list[dict],
    rag_context: str = "",
    conversation_context: str = "",
    client_context: str = "",
    custom_instructions: str = "",
    response_language: str = "auto",
) -> str:
    """Build the system prompt for code-interpreter mode.

    The prompt tells the LLM that attached files are mounted in /data and
    generated charts should be written to /output.
    """
    file_blocks = []
    for f in data_files:
        block = (
            f"- Nome: \"{f.get('name', 'file')}\"\n"
            f"  Percorso Python: {f.get('container_path', '/data/file')}\n"
            f"  Tipo: {f.get('type', 'sconosciuto')}"
        )
        preview = str(f.get("preview") or "").strip()
        if preview:
            block += f"\n  Anteprima limitata:\n{preview}"
        file_blocks.append(block)
    file_info = "\n".join(file_blocks)
    language_instruction = {
        "it": "Stampa risultati in italiano.",
        "en": "Print results in English.",
        "auto": "Stampa risultati nella stessa lingua della richiesta utente.",
    }.get(str(response_language or "auto").lower(), "Stampa risultati nella lingua richiesta dall'utente.")
    return (
        "Sei un assistente per l'analisi dati con Python.\n\n"
        "FILE DATI ALLEGATI:\n"
        f"{file_info}\n\n"
        "CONTESTO RAG DOCUMENTALE DA USARE COME RIFERIMENTO (non come dataset tabellare):\n"
        f"{rag_context or 'Nessun contesto documentale recuperato.'}\n\n"
        "CONTESTO CONVERSAZIONE:\n"
        f"{conversation_context or 'Nessun contesto conversazionale precedente.'}\n\n"
        "CONTESTO CLIENT:\n"
        f"{client_context or 'Nessun contesto client fornito.'}\n\n"
        "ISTRUZIONI AGGIUNTIVE:\n"
        f"{custom_instructions or 'Nessuna istruzione aggiuntiva.'}\n\n"
        "ISTRUZIONI:\n"
        "1. Genera codice Python che analizza i FILE DATI ALLEGATI.\n"
        "2. Usa sempre i Percorsi Python indicati sopra per leggere i file, ad esempio pandas.read_csv('/data/file.csv').\n"
        "3. Usa il CONTESTO RAG solo per regole, definizioni, interpretazione o vincoli aziendali rilevanti.\n"
        "4. Usa pandas/numpy per tabelle e calcoli; usa matplotlib per grafici.\n"
        "5. Per grafici: salva PNG in /output, ad esempio plt.savefig('/output/plot.png', bbox_inches='tight').\n"
        "6. Stampa risultati testuali con print(); non usare input interattivo.\n"
        f"7. {language_instruction}\n\n"
        "LIBRERIE CONSENTITE: pandas, numpy, matplotlib, json, csv, "
        "math, statistics\n\n"
        "QUESTO è IL TASK DELL'UTENTE: {user_query}\n\n"
        "RESPONDI SOLO CON CODICE PYTHON. Niente spiegazioni, niente markdown."
    ).format(user_query=user_query)
