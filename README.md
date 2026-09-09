# MOS Resource Guardian

Implementação inicial v0.1.0 em `mos-resource-guardian/`.

Estado: daemon e política de alertas testáveis; **não é ainda um pacote instalável pelo MOS Hub**.
Não altera CPU/RAM de VMs ou containers. Não garante prevenção de OOM/travamentos.

## Executar em modo observação

Requer Linux e Python 3.9+. Sem dependências externas.

```sh
python3 mos-resource-guardian/guardian.py --config mos-resource-guardian/config.json --database /caminho/persistente/guardian.db run
```

Consultar estado e histórico:

```sh
python3 mos-resource-guardian/guardian.py --database /caminho/persistente/guardian.db status
python3 mos-resource-guardian/guardian.py --database /caminho/persistente/guardian.db history
python3 -m unittest discover -s mos-resource-guardian/tests -v
```

Use um diretório persistente de dados, não a mídia de boot. Banco contém amostras a cada 5 segundos, com retenção de 7 dias. A consulta informa se o último registro está desatualizado. SIGTERM/SIGINT encerram o daemon; flock impede duas instâncias usando o mesmo banco. Configuração validada na inicialização; reinicie para aplicar alterações.

## Segurança e política

MemAvailable (não MemFree) determina margem de RAM. CPU calculada por deltas de /proc/stat sem duplicar guest/guest_nice. Alerta após 3 amostras sob pressão, emergência imediata abaixo de 1 GiB disponível; recuperação exige 6 amostras com margem adicional. Histórico e eventos SQLite com retenção. Estado inicial warming_up não implica host saudável.

Por padrão: margem RAM=max(2 GiB, 10% da RAM); reserva CPU=15%; recuperação exige mais 5 pontos percentuais e 512 MiB. Valores devem ser dimensionados para seu servidor. Um alerta não libera recursos: esta versão apenas observa.

## Próximas entregas (não implementadas)

- Empacotamento MOS Hub e página Vue integrada ao template oficial.
- Fase 2: libvirt com limites mínimos/máximos, detecção de balloon/hotplug e rollback.
- Fase 3: allowlist e prioridades Docker/LXC, sem reduzir memória abaixo do uso seguro.
- Fase 4: previsão de carga e perfis adaptativos com histórico suficiente.

Não baixar RAM nem desligar VMs automaticamente antes de validar os drivers do guest e a reserva do host. A promessa anterior de quatro fases completas ainda não foi cumprida.

## Referência MOS

O [guia oficial](https://docs.mos-official.net/docs/Plugins/MOS-Plugin-Development-Guide) usa página Vue e hooks no arquivo functions. Não presumir systemd: MOS é baseado em Devuan. Nenhum serviço é instalado automaticamente por este código.
