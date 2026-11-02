# Radar da Transparência Municipal — MVP gratuito + pedido manual

Primeira versão para validar o produto sem investir em infraestrutura ou integrar pagamentos.

## O que funciona

- Consulta de municípios pelo IBGE.
- Consulta exploratória ao SICONFI e ao PNCP.
- Tabelas e CSVs dos dados retornados.
- PDF demonstrativo gratuito, com limitações e fontes.
- Formulário que prepara uma solicitação para o relatório inicial de R$ 29.
- Entrega manual: o app **não recebe nem confirma pagamentos** e não libera automaticamente produto pago.

## Limitações importantes

- O resumo de despesas é exploratório e ainda precisa de validação contábil dos campos, granularidade e semântica do demonstrativo.
- O PNCP mostra apenas a primeira página de até 20 registros; a busca textual por município não garante cobertura completa.
- A IA generativa foi deixada de fora da primeira versão para reduzir custos e evitar conclusões sem suporte documental.
- O formulário de pedido não envia os dados do formulário automaticamente nem guarda pedidos em banco de dados. Se `CHAVE_ADMIN_RELATORIOS` estiver configurada, o responsável pode entrar com essa chave para gerar um PDF sem a marca de demonstração após confirmar manualmente o pagamento; isso não verifica Pix nem registra pedidos pagos. Se `LINK_PEDIDOS` estiver configurado, aparece um botão para abrir o canal de contato; o cliente ainda precisa copiar/baixar a solicitação e enviá-la por esse canal.
- Não usar como auditoria, laudo pericial ou prova de irregularidade.

## Rodar no Linux

1. Extraia o ZIP e abra um terminal dentro da pasta `radar_transparencia_prototipo`.
2. Instale Python 3.11 ou superior e crie o ambiente virtual:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. Execute:

   ```bash
   streamlit run app.py
   ```

4. O app abrirá no navegador. Escolha um município e exercício e consulte as abas.

## Trabalhar só pelo navegador

1. Crie uma conta gratuita no GitHub e um repositório vazio.
2. Envie os arquivos `app.py`, `requirements.txt`, `README.md` e `.gitignore` (e `.streamlit/secrets.toml.example`, se quiser).
3. Abra https://share.streamlit.io/ e conecte o repositório.
4. Selecione `app.py` como arquivo principal e publique.
5. Teste o endereço público com alguns municípios e exercícios antes de divulgar.

Os planos gratuitos têm limites e podem mudar. Não habilite gastos pagos sem conferir a tela de preços.

## Antes de divulgar a solicitação comercial

- Configure um canal de contato real e monitore as mensagens.
- Teste a consulta e valide manualmente os dados contra os portais oficiais.
- Produza um relatório de exemplo e deixe claro o conteúdo exato que o cliente receberá.
- Combine preço, prazo, forma de pagamento e política de correção/reembolso antes do pagamento.
- Confirme o pagamento diretamente no serviço bancário; nunca confie em captura de tela enviada pelo cliente. A chave de administrador libera somente a geração manual do PDF e não confirma pagamentos.
- Entregue o PDF manualmente após confirmar o recebimento.
- Verifique suas obrigações fiscais e os termos do serviço de pagamento utilizado.

## Próximas melhorias (só depois de validar demanda)

1. Testes automatizados para os cálculos e para o PDF.
2. Validação da semântica SICONFI e paginação/filtros PNCP.
3. Registro persistente de pedidos e processo de entrega.
4. Integração de pagamento com verificação server-side, webhook autenticado, idempotência e reconciliação.
5. IA opcional para explicar evidências já calculadas e verificadas, sem criar números ou acusar ilegalidade.
