"""Popula um banco de DEMONSTRAÇÃO com relatórios sintéticos (sem dados reais). Uso: CIO_ENV=dev python scripts/seed_demo.py"""
from datetime import datetime, timedelta, timezone

from cio.config import get_settings
from cio.db import connect, migrate
from cio.ingest import run_once


class Demo:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.msgs = []
        tipos = [
            ("🔵 Zoho Desk – Resumo diário de tickets – {d}", "luis.miguel@europcar.com.br",
             "<h2>Zoho Desk — Exemplo</h2><p>42 tickets não fechados: 10 em aberto, 3 escalados.</p>"
             "<table><tr><td>10</td><td>Em aberto</td></tr><tr><td>3</td><td>Escalados</td></tr></table>"),
            ("🔵 Relatório de reservas — {d} | Exemplo", "callcenter@europcar.com.br",
             "<h2>Reservas (sintético)</h2><p>Reservas confirmadas: 37. Canceladas: 4. Estação líder: GRU.</p>"),
            ("🔵 [Atendimento Europcar] 3 alertas - tickets #1001, #1002, #1003", "callcenter@europcar.com.br",
             "<p>Alerta sintético: cliente aguardando estorno de caução há 12 dias.</p><script>alert(1)</script>"),
            ("🔵 [Diagnóstico Europcar] {d} - 20/60 tickets auditados", "callcenter@europcar.com.br",
             "<p>Diagnóstico sintético de qualidade de atendimento.</p>"),
        ]
        self.extra = [("🔵 Fechamento semanal de frota — semana 45", "callcenter@europcar.com.br",
                       "<p>Tipo novo (sem regra): deve cair na fila de revisão.</p>")]
        n = 0
        tipos.append(("🔵 Agente COMPLETO INTEGRAL {d} - 2 itens para ação humana", "callcenter@europcar.com.br",
                      "<p>Rodada completa (exemplo). 1) Ticket #1500: sexto lembrete enviado ao país. "
                      "2) Ticket #1510: cliente contesta cobrança de franquia; decisão humana.</p>"))
        for day in range(21):
            d = now - timedelta(days=day, hours=1)
            for subj, sender, html in tipos:
                n += 1
                from cio.zoho import MailMessage
                m = MailMessage("demo", str(n), "f", sender, subj.format(d=d.strftime("%d/%m/%Y")),
                                int(d.timestamp() * 1000) - n, False)
                self.msgs.append((m, html))
        from cio.zoho import MailMessage
        for subj, sender, html in self.extra:
            n += 1
            self.msgs.append((MailMessage("demo", str(n), "f", sender, subj, int(now.timestamp() * 1000) - 5000, False), html))
        self.content = {m.message_id: h for m, h in self.msgs}

    def search_messages(self, a, sender, since):
        return iter([m for m, _ in self.msgs if m.sender == sender])

    def get_content(self, m): return self.content[m.message_id]
    def get_original(self, m): return b""
    def list_attachments(self, m): return []
    def excluded_folder_ids(self, a): return set()


s = get_settings()
s.mail_account_ids = ["demo"]
conn = connect(s)
migrate(conn, s.authorized_senders)
print(run_once(conn, Demo(), s, since_ms=0))
for email, role, areas, name in [("admin@europcar.com.br", "admin", "", "Administrador (exemplo)"),
                                 ("gestor@europcar.com.br", "gestor", "atendimento,comercial", "Gestor (exemplo)"),
                                 ("operador@europcar.com.br", "operacional", "atendimento", "Operador (exemplo)")]:
    conn.execute("INSERT INTO users(email, role, name) VALUES (?,?,?) ON CONFLICT (email) DO NOTHING", (email, role, name))
    for a in filter(None, areas.split(",")):
        conn.execute("INSERT INTO user_areas SELECT u.id, a.id FROM users u, areas a WHERE u.email=? AND a.slug=? ON CONFLICT DO NOTHING", (email, a))
