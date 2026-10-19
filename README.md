# UNIVERSAL PDF EDITOR

Editor web de PDFs com edição direta por clique:
**CARREGAR → CLICAR → EDITAR → SALVAR → BAIXAR.**

O texto existente é identificado, alterado no próprio arquivo e um novo PDF é gerado. Antes de
qualquer download, o arquivo gerado é reaberto e conferido automaticamente; se a verificação falhar,
nada é baixado.

---

## Executar

Requisitos: **Python 3.10+**, **Node.js 18+** e, para PDFs digitalizados, **Tesseract OCR**
(`sudo apt install tesseract-ocr` · `brew install tesseract`). Os modelos de português e inglês já
acompanham o projeto em `backend/tessdata/`.

```bash
./start.sh
```

Abra **http://localhost:8000**. Na primeira execução o script cria `.venv`, instala as dependências
e compila a interface. Porta e host: `PORT=9000 HOST=0.0.0.0 ./start.sh`.

Com Docker (inclui Tesseract e fontes métricas compatíveis):

```bash
docker build -t universal-pdf-editor .
docker run -p 8000:8000 universal-pdf-editor
```

Desenvolvimento com recarga automática (dois terminais):

```bash
cd backend && uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev        # http://localhost:5173 (proxy para /api)
```

PDFs de exemplo: `python scripts/make_samples.py` → pasta `samples/`.

Configuração: copie `.env.example` para `.env` (limites de tamanho/páginas, tempo de expiração da
sessão, idiomas do OCR, tolerância da verificação).

## Testes

```bash
cd backend && python -m pytest -q          # motor, API e processos isolados (50 testes)
cd .. && python -m pytest e2e -q            # navegador real (Playwright): 6 testes
```

Os testes de interface precisam de `pip install playwright && playwright install chromium` e da
interface compilada (`cd frontend && npm run build`). Capturas de tela ficam em `e2e/screenshots/`.

Cada teste de persistência segue o roteiro: abrir → identificar texto → editar → salvar → **reabrir
o PDF exportado** → conferir que a alteração está lá, que o texto antigo sumiu, que nada mudou fora da
área editada (comparação de pixels) e que as demais páginas estão idênticas.

| Tipo de PDF testado | Teste | Modo |
|---|---|---|
| Texto nativo selecionável (Helvetica, Times, Courier, negrito, cor) | `test_native_*`, `test_bold_*`, `test_gray_*` | A |
| Digitalizado (somente imagem) | `test_scanned_pdf_ocr`, `test_scanned_flow_via_api`, e2e | B |
| Digitalizado com camada OCR invisível | `test_scanned_with_invisible_ocr_layer` | B/C |
| Com imagens | `test_pdf_with_images`, `test_delete_existing_image` | A |
| Várias páginas | `test_multipage` | A |
| Tabelas (célula alinhada à direita, texto longo sem invadir coluna, células separadas por borda) | `test_table_cell_right_aligned`, `test_longer_text_is_fitted`, `test_table_border_splits_adjacent_cells` | A |
| Fontes incorporadas (subset Identity-H) | `test_embedded_font_reused`, `test_embedded_font_missing_glyphs_fallback` | A |
| Texto pequeno (5 pt) | `test_small_text` | A |
| Paisagem e página com /Rotate 90 | `test_landscape_page`, `test_rotated_page`, `test_add_text_on_rotated_page_is_upright` | A |
| Elementos sobrepostos (fundo colorido, texto sobre imagem, entrelinha apertada) | `test_overlapping_*`, `test_text_over_image`, `test_tight_line_spacing_*` | A |
| Parágrafos (detecção, quebra automática, justificado, preserva quebras originais) | `test_paragraph_*`, `test_table_columns_not_grouped_as_paragraph`, e2e | A |
| Formulários (texto, caixa de seleção, lista; campo somente leitura recusado) | `test_form_fields_*`, `test_readonly_field_rejected`, e2e | Form |
| Cirílico, grego, CJK, aspas tipográficas; hebraico (direita→esquerda) | `test_non_latin_scripts_edit`, `test_rtl_text_edit` | A |
| Digitalização inclinada e digitalização apagada | `test_skewed_scan_*`, `test_faint_scan_is_recognized` | B |
| Texto minúsculo sobre imagem grande (clique) | e2e `test_tiny_text_over_large_image_is_clickable` | A |
| Processo auxiliar que trava/cai não derruba o servidor | `test_workers.py` | — |
| Protegido por senha, assinatura digital, arquivo inválido, limite de tamanho | `test_api.py` | — |
| Falha de geração nunca entrega arquivo | `test_verification_failure_blocks_download` | — |

Os PDFs de teste são gerados por código (`backend/tests/pdf_factory.py`).

### Compatibilidade medida com PDFs reais

`backend/tools/compat_harness.py` roda o ciclo completo (abrir → editar 2 trechos → exportar →
verificar) em qualquer pasta de PDFs, cada arquivo em processo isolado com tempo limite:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/mozilla/pdf.js corpus
(cd corpus && git sparse-checkout set --no-cone '/test/pdfs/*.pdf')
cd backend && python tools/compat_harness.py ../corpus/test/pdfs --out relatorio.json
```

Resultado no acervo de testes do PDF.js (999 arquivos reunidos justamente por serem problemáticos):

| | Arquivos |
|---|---|
| Editados e aprovados na verificação | **506** |
| Falharam na verificação (arquivo **não** entregue) | 28 |
| Erro ao abrir/estrutura inválida (árvore de páginas cíclica etc.) | 4 |
| Tempo limite | 1 |
| Sem texto para editar (só imagens/vetores, páginas vazias) | 431 |
| Protegidos por senha / sem páginas / ilegíveis | 29 |

**Taxa de sucesso entre os que têm texto editável: 93,9% (506 de 539).** As falhas restantes são
emojis, escritas raras (ex.: húngaro antigo), alguns árabes com fontes CID, CropBox fora da MediaBox
e PDFs em que remover um texto altera desenhos que usam o texto como máscara — em todos esses casos
a verificação bloqueia a entrega.

Para validar com seus próprios documentos, rode o mesmo comando na pasta deles.

## Como funciona

```
frontend (React + TypeScript + Tailwind)          backend (FastAPI + PyMuPDF + pikepdf + OpenCV + Tesseract)
 ├─ upload / arrastar e soltar         ─────────▶  POST /api/documents          sessão isolada (UUID, dir temporário)
 ├─ páginas renderizadas pelo servidor ◀─────────  GET  /pages/{n}/image        renderização MuPDF (fiel ao original)
 ├─ caixas clicáveis sobre o texto     ◀─────────  GET  /pages/{n}/analysis     elementos de texto + modo da página
 ├─ edição no local + histórico        ─────────▶  POST /pages/{n}/preview      aplica as operações de verdade e renderiza
 └─ Salvar e baixar                    ─────────▶  POST /export                 gera, reabre, verifica → link de download
```

**Estado centralizado:** toda alteração é uma operação (`edit_text`, `delete_element`, `add_text`,
`add_image`, `set_field`) numa lista única com desfazer/refazer. A prévia na tela é o PDF realmente modificado pelo
servidor (não uma sobreposição desenhada no navegador); o mesmo código gera o arquivo final.

### Modo A — texto nativo
1. Extração dos caracteres com posição, fonte, tamanho, cor e direção; agrupamento em elementos por
   linha, separando mudanças de estilo e colunas (lacunas grandes).
2. Detecção de alinhamento (esquerda/direita/centro, por comparação com elementos vizinhos) e do
   espaço disponível até o próximo texto **ou borda de tabela**.
3. Os caracteres originais são removidos do content stream com redação restrita a texto
   (imagens, fundos e linhas permanecem intactos).
4. O novo texto é escrito, nesta ordem de preferência:
   - com o **mesmo objeto de fonte do PDF** (reaproveitando glifos do subset via `/ToUnicode`, com
     checagem de que o glifo realmente existe no programa da fonte);
   - com a fonte incorporada extraída, se tiver os glifos;
   - com a equivalente do sistema (Arial→Liberation Sans, Calibri→Carlito…), com escala horizontal
     para reproduzir a largura original;
   - com a fonte padrão PDF do mesmo estilo.
   - para escritas não latinas, uma fonte do sistema com cobertura do caractere (fontconfig) ou a fonte
     CJK embutida no MuPDF; árabe e hebraico são compostos com HarfBuzz (formas e ordem corretas).
5. Texto maior que o espaço é comprimido (até 82% de largura e 75% de tamanho); se ainda assim não
   couber, o relatório avisa.
6. **Parágrafos:** linhas corridas do mesmo estilo viram um único bloco editável. O texto novo é
   redistribuído na largura original (quebra automática, justificado quando o original era), mantendo as
   quebras de linha originais que continuam válidas — uma correção pequena não reorganiza o parágrafo.
   Se o texto crescer, usa o espaço livre abaixo; se faltar, reduz levemente a fonte e avisa.
7. Texto de anotações e campos não é tratado como conteúdo da página. **Campos de formulário**
   (texto, caixa de seleção, lista) são preenchidos como campos de verdade; somente leitura é respeitado.
8. Trechos cuja fonte não tem Unicode válido (caracteres-lixo na extração) são lidos por OCR da própria
   região e editados por reconstrução visual.

### Modo B — digitalizados (OCR)
Tesseract (por+eng) detecta palavras/linhas com coordenadas; as palavras são agrupadas em elementos.
Antes do reconhecimento: realce de contraste (CLAHE) em digitalizações apagadas e **correção de
inclinação** (perfil de projeção, até ±6°); o texto reescrito acompanha a inclinação original.
Cor do texto, cor do fundo, tamanho e negrito são estimados a partir dos pixels. Páginas com camada
OCR invisível existente são reconhecidas e usam essa camada diretamente.

### Modo C — reconstrução visual de regiões
Quando o texto não é texto nativo (imagem, vetores, fonte sem Unicode), a região é renderizada em alta
resolução, os pixels do texto são removidos (preenchimento com o fundo ou *inpainting* OpenCV) e o
remendo é inserido exatamente sobre a região. O novo texto é escrito como texto vetorial real
(selecionável). A interface e o relatório informam quando uma edição usou reconstrução visual.

### Verificação antes do download
1. Estrutura válida em dois leitores independentes (MuPDF e qpdf/pikepdf).
2. Mesmo número de páginas.
3. Cada texto novo presente na posição esperada; cada texto substituído ausente.
4. Comparação de pixels: páginas não editadas idênticas; nas editadas, nada muda fora das regiões
   alteradas (tolerância configurável).
5. Se um texto não puder ser confirmado com a fonte original, o arquivo é regerado com fonte
   equivalente e verificado de novo. Se ainda falhar: erro específico, nenhum arquivo, botão
   “Tentar novamente”.

### Desempenho e isolamento
- Análise, OCR, prévia e exportação rodam em um **pool de processos** (`UPE_WORKERS`, padrão até 4):
  vários documentos em paralelo e um PDF malformado que derrube o MuPDF afeta só o processo auxiliar,
  que é recriado automaticamente. Cada tarefa tem tempo limite (`UPE_TASK_TIMEOUT_S`).
- Renderizações das páginas originais ficam em cache em disco durante a sessão.
- As páginas são analisadas sob demanda (ao aparecerem na tela).

### Segurança
- Somente PDFs válidos (assinatura `%PDF-` + abertura pelo MuPDF); limites de tamanho, páginas e
  imagens configuráveis.
- Cada documento em diretório próprio; apagado ao fechar, após o tempo de inatividade e ao reiniciar.
- PDFs com senha exigem a senha; restrições de modificação do autor são respeitadas (é preciso a senha
  de proprietário). A proteção original é mantida no arquivo exportado.
- Documentos assinados: aviso no upload e confirmação obrigatória antes de exportar; o arquivo
  gerado recebe `Producer`/`ModDate` atualizados, sem se apresentar como o original assinado.

## Estrutura

```
universal-pdf-editor/
├── start.sh · Dockerfile · .env.example · README.md
├── backend/
│   ├── requirements.txt · requirements-dev.txt · pytest.ini
│   ├── tessdata/                 modelos OCR (por, eng)
│   ├── app/
│   │   ├── main.py               API FastAPI + serve a interface compilada
│   │   ├── config.py             configuração (variáveis UPE_*)
│   │   ├── storage.py            sessões isoladas, validação, senha, assinatura, limpeza
│   │   ├── workers.py            pool de processos isolados (análise, OCR, prévia, exportação)
│   │   └── engine/
│   │       ├── analysis.py       Modo A: elementos, alinhamento, espaço disponível
│   │       ├── fonts.py          identificação e escolha de fontes
│   │       ├── pdffont.py        reaproveitamento do objeto de fonte original (subset)
│   │       ├── ocr.py            Modo B: Tesseract + estimativa de estilo
│   │       ├── raster.py         Modo C: reconstrução visual de regiões
│   │       ├── editor.py         aplicação das operações ao PDF
│   │       └── verify.py         geração + verificação do arquivo final
│   ├── tools/compat_harness.py   medição de compatibilidade em acervos de PDFs reais
│   └── tests/                    pdf_factory.py · test_engine.py · test_api.py · test_workers.py
├── frontend/                     React + TypeScript + Tailwind (Vite)
│   └── src/  App.tsx · lib/{api,history,text}.ts · components/{UploadScreen,Editor,PageView,InlineEditor,SaveDialog,Logo}.tsx
├── e2e/test_ui.py                testes no navegador (Playwright)
└── scripts/make_samples.py       PDFs de exemplo
```

Escolhas de arquitetura: as páginas são renderizadas pelo MuPDF no servidor em vez do PDF.js, para que
a imagem exibida e as coordenadas dos elementos venham do mesmo motor que grava o arquivo (sem
desalinhamento). A interação é feita com DOM/CSS sobre a página; Konva.js não foi necessário.

## Limitações conhecidas (honestas)

- **Nenhum editor edita 100% dos PDFs.** Veja a taxa medida acima; quando a verificação não confirma
  uma edição, o arquivo não é entregue.
- Fontes Type3 e CFF puras não são reaproveitadas diretamente; usa-se a equivalente mais próxima, e a
  forma das letras pode diferir levemente.
- Emojis não têm fonte disponível no servidor; letras matemáticas estilizadas (𝑥) são escritas como
  letras comuns.
- No OCR, a fonte de substituição é sans/serif genérica; manuscritos e digitalizações muito degradadas
  podem não ser reconhecidos. Inclinação acima de ±6° não é corrigida.
- Texto em ângulos não múltiplos de 90° é editável, mas a caixa de edição na tela é horizontal.
- Detecção de parágrafos é conservadora: textos em colunas, listas e linhas curtas continuam sendo
  editados linha a linha.
- Árabe: a extração de texto de alguns glifos compostos é imperfeita; a verificação dessas edições
  compara o conjunto de caracteres em vez do texto exato.
