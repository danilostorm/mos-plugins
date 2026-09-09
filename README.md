# MOS Resource Guardian — 0.5.0

## Novidade: integração automática com VMs do MOS

No Guardian, marque **Usar CPU/RAM da tela de criação e edição como tetos**, escolha o modo e salve uma vez. As VMs KVM persistentes atuais e futuras são descobertas por UUID. Configurar 16 CPUs e 16 GB no editor MOS fornece automaticamente esses tetos ao Guardian, sem duplicar cadastro. A definição salva é relida a cada 10 segundos; o balloon atual não altera o teto importado.

O painel permite excluir VMs; configurações manuais por nome/UUID têm precedência. A atualização não ativa essa opção automaticamente. Em observação, só consulta e mostra propostas; no modo automático, também habilita estatísticas live do balloon.

CPU usa quota global mantendo a quantidade de vCPUs selecionada. **Core Pinning deve ficar desmarcado no MOS** para o escalonador escolher os processadores físicos. Pinning existente é informado e preservado, nunca removido silenciosamente. RAM dinâmica requer balloon virtio e estatísticas recentes. Para 16 GiB, o mínimo automático é 4 GiB; para outras capacidades é metade do teto, limitado à faixa 1–4 GiB e nunca acima do teto. Para mudar o mínimo, use um alvo manual.

Com margem no host, RAM sobe quando a folga do guest fica abaixo de 512 MiB e desce gradualmente quando supera 1280 MiB, respeitando o piso. Entre esses valores mantém a alocação. Mantém cooldown e verificação da versão anterior. Guests sem balloon ou com hugepages/memória travada recebem apenas controle de CPU.

**Não injeta controles no formulário nativo nem reescreve XML.** A integração é pela leitura da definição salva: continua funcionando após editar pelo MOS, sem depender de alterações personalizadas que o editor pode apagar. A VM ainda pode iniciar com toda a RAM configurada antes do ajuste por balloon; não existe reserva/admissão automática de recursos no momento do boot.

Atualize para **v0.5.0** pelo MOS e recarregue o painel. Instruções anteriores abaixo continuam válidas, substituindo a versão do pacote por 0.5.0.

Plugin de gerenciamento local de CPU/RAM para MOS, com painel nativo, daemon Python, pacote Debian e controles opt-in para libvirt, Docker e LXC. A versão 0.5.0 é uma **prévia para validação em hardware MOS**.

## Fases implementadas

| Fase | Implementação |
| --- | --- |
| 1 — Host | Monitor CPU/MemAvailable, reservas, emergência, histerese, SQLite, logs rotativos, API privada, painel Vue, pacote .deb e hooks MOS/SysV |
| 2 — VMs | Quota global de CPU; hotplug opcional; balloon de RAM com estatísticas recentes, mínimos/máximos e margem do guest; journal e confirmação posterior |
| 3 — Containers | Docker/LXC identificados pelo processo ativo e cgroup v2; cpu.max, memory.high, allowlist, prioridades, pausa e restauração |
| 4 — Previsão | Regressão linear sobre amostras contínuas, horizonte configurável, rejeição de lacunas, perfis adaptativo/conservador/equilibrado, redução preventiva e recuperação gradual |

O daemon inicia em **Observar**, sem alvos. Cadastre os alvos, revise as decisões propostas e selecione **Aplicar automaticamente**. Salvar aplica a política validada.

## Instalação pelo MOS Hub

1. Adicione `https://github.com/danilostorm/mos-plugins` aos repositórios do MOS Hub.
2. Procure **MOS Resource Guardian** e selecione a release `v0.5.0` (pré-release).
3. Abra **Plugins → MOS Resource Guardian**.
4. Escolha um diretório de histórico em disco persistente, como `/mnt/SEU-DISCO/guardian`, salve e reinicie com `/etc/init.d/mos-resource-guardian restart`.
5. Cadastre nomes/IDs de VMs e containers, limites e prioridades. Observe as decisões antes de ativar o modo automático.

O instalador MOS deriva parte da identidade do nome do repositório: o identificador interno é `plugins`, compatível com `mos-plugins`. O nome visível continua MOS Resource Guardian. Este repositório distribui um plugin; não é um agregador de vários pacotes independentes.

Releases são geradas pelo GitHub Actions depois dos testes. Confira [Actions](https://github.com/danilostorm/mos-plugins/actions) se a release ainda não estiver disponível. O workflow não sobrescreve releases existentes: alterações posteriores exigem incrementar a versão.

### Instalação manual

Baixe o pacote em [Releases](https://github.com/danilostorm/mos-plugins/releases) e execute como root:

```sh
apt install ./mos-resource-guardian_0.5.0_all.deb
/etc/init.d/mos-resource-guardian status
```

Para MOS com rootfs recriado no boot, prefira o Hub: ele preserva o pacote e os hooks em `/boot/optional/plugins/plugins`. Instalação manual não registra a release no Hub.

## Reservas e controle

- Padrão: reserva RAM = maior entre 2 GiB e 10% do total; CPU = 15%. Emergência abaixo de 1 GiB disponível.
- Pressão após 3 amostras; recuperação após 6 amostras com margem adicional de 512 MiB/5 pontos de CPU.
- Um ajuste global a cada 60 segundos, incluindo após reinício. CPU: passos de 0,5 CPU equivalente ou uma vCPU. RAM: passos de 256 MiB.
- Prioridade 1 cede primeiro; 100 recupera primeiro. Só a allowlist é alterada.
- Crescimento de RAM de VM exige memória **já disponível** no host; liberação futura não é contabilizada.
- Os mínimos/máximos delimitam destinos dos ajustes. Não são reserva física instantânea nem bloqueio da inicialização de novas cargas.
- O perfil conservador impede aumentos automáticos. O adaptativo faz isso quando prevê pressão e pode reduzir preventivamente.
- A previsão é estatística local; não usa IA externa nem aprendizado sazonal.

## Compatibilidade

**VM:** libvirt em `qemu:///system`. Quota global exige `global_period/global_quota` em `virsh schedinfo`. Hotplug depende da configuração da VM; remoção exige vCPU declarada hotpluggable. Não altera XML persistente, topologia, pinning ou máximo de CPUs. Restaure os limites antes de trocar quota por hotplug ou vice-versa.

Balloon requer virtio, driver no guest e `dommemstat` com `actual`, `usable` e `last_update` de até 30 segundos. Ative estatísticas, se necessário:

```sh
virsh --connect qemu:///system dommemstat NOME-DA-VM --period 5 --live
```

Guests sem estatísticas, com hugepages ou memória travada não recebem redução de RAM. Retorno de setmem/setvcpus não é confirmação: o daemon consulta o estado e bloqueia ajustes se não confirmar em 30 segundos.

**Docker/LXC:** exige cgroup v2 dedicado com controladores CPU/memória e cpuset efetivo. Resolve PID e identidade do runtime; rejeita o cgroup do próprio daemon/host. Para LXC fora do diretório padrão, preencha o diretório no painel. Mudanças são de runtime, sem alterar Compose/config; reinícios podem restaurar limites do runtime original.

`memory.high` é controle **suave de pressão/reclaim**, não teto rígido; pode ser ultrapassado. O plugin não reduz memory.max nem mata ou pausa VMs/containers. Não há garantia absoluta contra falta de memória/travamentos, especialmente com cargas fora da allowlist, crescimento súbito ou mínimos incompatíveis com o host.

## Pausa, restauração e conflitos

Pausar interrompe ajustes, mantendo limites aplicados. **Restaurar limites anteriores** pausa o controlador e restaura gradualmente o primeiro valor observado de cada recurso. RAM de VM aguarda margem real no host e guest.

O journal é salvo **antes** da escrita. Identidade, valor anterior, destino, resultado e erro ficam registrados. Pedido pendente após queda do daemon bloqueia alterações até restauração/reconciliação. Se outro administrador mudou o limite, o Guardian não sobrescreve: aponta conflito. Consulte `before` no registro, restaure manualmente e solicite restauração novamente. Identidades reiniciadas expiram registros antigos.

Restaure e aguarde a conclusão antes de remover alvos, trocar método CPU ou desinstalar. Remover alvo não apaga seu journal; ele pode ser restaurado. Parar/desinstalar não aumenta limites automaticamente durante emergência.

## Dados e operações

| Item | Caminho |
| --- | --- |
| Configuração | /boot/optional/plugins/plugins/settings.json |
| Banco padrão | /var/lib/mos-resource-guardian/guardian.db |
| Socket privado (0600) | /run/mos-resource-guardian/api.sock |
| Logs rotativos | /run/mos-resource-guardian/events.log* |
| Painel | /var/www/mos-plugins/plugins |
| Comando API | /usr/bin/plugins/mos-resource-guardian |

Configure `data_directory` em disco persistente se /var/lib for volátil. Alterar exige reinício e não move histórico/journal: **restaure antes da troca**, ou mova o banco com daemon parado. Para /mnt, o launcher recusa iniciar sem volume montado.

Amostras/eventos: 7 dias por padrão. Journal mantém baseline e último ajuste para restauração; intermediários/resolvidos expiram. Logs: até 3 arquivos de 1 MiB. O pacote não apaga banco ao desinstalar; o MOS pode remover a configuração, então exporte-a antes.

```sh
/usr/bin/plugins/mos-resource-guardian status
/usr/bin/plugins/mos-resource-guardian config
/usr/bin/plugins/mos-resource-guardian history
/usr/bin/plugins/mos-resource-guardian pause
/usr/bin/plugins/mos-resource-guardian restore
/etc/init.d/mos-resource-guardian restart
```

O painel usa a API autenticada do MOS. Não abre TCP, envia telemetria ou aceita shell arbitrário. Edição direta do arquivo exige reinício; Salvar usa validação e substituição atômica.

## Desenvolvimento e validação

```sh
python3 -m unittest discover -s mos-resource-guardian/tests -v
node --test page/tests/federation.test.js
python3 packaging/build.py
```

Sem dependências pip/npm. remoteEntry.js usa o Vue compartilhado do MOS; Plugin.vue referencia a mesma implementação, sem CDN.

Testes cobrem política, previsão/lacunas, limites, observação, cooldown, prioridade, journal, falhas, reinícios, conflitos, restauração, contrato do painel e socket. Localmente, onde sockets são proibidos, só esse teste é pulado; no CI ele é obrigatório.

**Não foi realizado teste em seu servidor.** Validar em MOS real: instalação/remoção Hub, painel, boot com disco de dados, quota de VM de teste, balloon, Docker/LXC e restauração.

## Referências

- [Guia MOS](https://docs.mos-official.net/docs/Plugins/MOS-Plugin-Development-Guide/)
- [Instalador/API MOS](https://github.com/mos-nas/mos-api/blob/master/src/services/plugins.service.js)
- [Carregamento do painel MOS](https://github.com/mos-nas/mos-frontend/blob/master/src/composables/usePlugins.ts)
- [Libvirt](https://www.libvirt.org/manpages/virsh.html)
- [Cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
