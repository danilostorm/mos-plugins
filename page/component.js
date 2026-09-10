export function encodeConfig(value) {
  return 'b64:' + btoa(Array.from(new TextEncoder().encode(JSON.stringify(value)), b => String.fromCharCode(b)).join(''));
}
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export function confirms(actual, expected) {
  if (Array.isArray(expected)) return Array.isArray(actual) && actual.length === expected.length && expected.every((v, i) => confirms(actual[i], v));
  if (expected && typeof expected === 'object') return !!actual && Object.keys(expected).every(k => confirms(actual[k], expected[k]));
  return actual === expected;
}
export function createPlugin({ h, ref, onMounted, onUnmounted }) {
  return {
    name: 'ResourceGuardian',
    setup() {
      const state = ref(null), config = ref(null), history = ref([]);
      const error = ref(''), notice = ref(''), busy = ref(false);
      const healthError = ref(''), saved = ref(''), vms = ref(null);
      let timer, stopped = false, polling = false;
      async function api(op, payload) {
        const args = payload === undefined ? [op] : [op, encodeConfig(payload)];
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 65000);
        try {
          const response = await fetch('/api/v1/mos/plugins/query', {
            method: 'POST', signal: controller.signal,
            headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('authToken') },
            body: JSON.stringify({ command: 'mos-resource-guardian', args, timeout: 60, parse_json: true })
          });
          const data = await response.json();
          if (!response.ok || data.success === false) throw new Error(data.error || data.output || 'Falha na operação');
          const result = typeof data.output === 'string' ? JSON.parse(data.output) : data.output;
          if (!result || result.error) throw new Error(result?.error || 'Resposta inválida');
          return result;
        } finally { clearTimeout(timeout); }
      }
      async function refresh() {
        if (polling || stopped) return;
        polling = true;
        try {
          state.value = await api('status');
          if (Array.isArray(state.value.auto_vms)) vms.value = state.value.auto_vms;
          history.value = await api('history');
          healthError.value = '';
        } catch (e) { healthError.value = String(e.message); }
        finally { polling = false; }
      }
      async function action(op, payload) {
        if (busy.value) return;
        busy.value = true; notice.value = ''; error.value = '';
        const requested = payload === undefined ? undefined : JSON.parse(JSON.stringify(payload));
        try {
          await api(op, requested);
          const persisted = await api('config');
          if (op === 'configure' && !confirms(persisted, requested)) {
            throw new Error('O serviço não confirmou os valores enviados. Suas alterações continuam no painel. Atualize o plugin e reinicie o serviço.');
          }
          config.value = persisted;
          saved.value = canonical(persisted);
          notice.value = op === 'restore' ? 'Restauração solicitada. Aguarde a verificação de cada ajuste.' : 'Configuração salva e confirmada pelo serviço.';
          await refresh();
        } catch (e) { error.value = String(e.message); }
        finally { busy.value = false; }
      }
      async function discover() {
        if (busy.value) return;
        busy.value = true;
        try { vms.value = (await api('discover')).vms; healthError.value = ''; }
        catch (e) { healthError.value = 'Falha ao buscar VMs: ' + e.message; }
        finally { busy.value = false; }
      }
      onMounted(async () => {
        try { config.value = await api('config'); saved.value = canonical(config.value); } catch (e) { error.value = e.message; }
        await refresh();
        await discover();
        if (!stopped) timer = setInterval(refresh, 5000);
      });
      onUnmounted(() => { stopped = true; clearInterval(timer); });
      const button = (label, fn) => h('button', { type: 'button', disabled: busy.value, onClick: fn }, label);
      const number = (label, obj, key, min, step = 1) => h('label', [label, h('input', {
        type: 'number', min, step, value: obj[key], onInput: e => { obj[key] = Number(e.target.value); }
      })]);
      const select = (label, obj, key, options) => h('label', [label, h('select', {
        value: obj[key], onChange: e => { obj[key] = e.target.value; }
      }, options.map(([value, text]) => h('option', { value }, text)))]);
      const fmt = value => value == null ? '—' : Number(value).toFixed(1);
      const table = (heads, rows) => h('div', { class: 'rg-scroll' }, h('table', [
        h('thead', h('tr', heads.map(x => h('th', x)))), h('tbody', rows.map(row => h('tr', row.map(x => h('td', String(x ?? '—'))))))
      ]));
      function addTarget() {
        config.value.targets.push({ kind: 'vm', id: '', priority: 50, cpu_method: 'quota', cpu_min: 1, cpu_max: 4,
          manage_memory: false, memory_min_mib: 2048, memory_max_mib: 4096, memory_headroom_mib: 512 });
      }
      return () => h('section', { class: 'resource-guardian' }, [
        h('style', `.resource-guardian{max-width:1250px;margin:auto;padding:24px;font-family:inherit}.resource-guardian h2{font-size:26px;margin:0 0 8px}.resource-guardian h3{margin:24px 0 12px}.resource-guardian p{margin:8px 0 16px}.resource-guardian .rg-grid{display:flex;flex-wrap:wrap;gap:16px}.resource-guardian .rg-card,.resource-guardian fieldset{padding:16px;border:1px solid #78909c66;border-radius:10px;margin-bottom:12px}.resource-guardian .rg-card{flex:1;min-width:180px}.resource-guardian strong{display:block;font-size:24px}.resource-guardian label{display:flex;flex-direction:column;gap:6px;min-width:140px;margin:6px 0}.resource-guardian input,.resource-guardian select{border:1px solid #78909c88;border-radius:6px;padding:8px;background:transparent;color:inherit;max-width:240px}.resource-guardian option{color:#17212b;background:#fff}.resource-guardian button{border:1px solid #78909c88;border-radius:6px;padding:8px 14px;margin:4px 8px 4px 0;color:inherit;background:#78909c22;cursor:pointer}.resource-guardian button:disabled{opacity:.5}.resource-guardian .rg-error{padding:12px;border-left:4px solid #e57373;background:#e5737322}.resource-guardian .rg-notice{padding:12px;border-left:4px solid #64b5f6;background:#64b5f622}.resource-guardian table{width:100%;border-collapse:collapse}.resource-guardian th,.resource-guardian td{text-align:left;padding:10px;border-bottom:1px solid #78909c44;white-space:nowrap}.resource-guardian .rg-scroll{overflow:auto}.resource-guardian input[type=checkbox]{width:20px;height:20px}.resource-guardian small{display:block;opacity:.8}`),
        h('style', '.resource-guardian .rg-save{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px;background:#172127;border:1px solid #78909c;border-radius:8px}.resource-guardian summary{cursor:pointer;padding:16px 0;font-weight:bold}.resource-guardian fieldset:disabled{opacity:.7}.resource-guardian label:has(input[type=checkbox]){flex-direction:row-reverse;justify-content:flex-end;align-items:center;gap:12px;padding:10px 0}.resource-guardian input[type=checkbox]{flex-shrink:0;accent-color:#70b5ca}.resource-guardian legend{padding:0 8px}'),
        h('h2', 'MOS Resource Guardian'),
        h('p', 'v0.6.1 • Escolha suas VMs, ative o gerenciamento e salve.'),
        error.value && h('div', { role: 'alert', class: 'rg-error' }, error.value),
        healthError.value && h('div', { role: 'alert', class: 'rg-error' }, healthError.value),
        notice.value && h('div', { role: 'status', class: 'rg-notice' }, notice.value),
        state.value && h('div', { class: 'rg-grid' }, [
          h('div', { class: 'rg-card' }, ['Estado', h('strong', state.value.stale ? 'Desatualizado' : state.value.state), h('small', 'Modo: ' + state.value.mode)]),
          h('div', { class: 'rg-card' }, ['CPU ocupada', h('strong', fmt(state.value.cpu_percent) + '%')]),
          h('div', { class: 'rg-card' }, ['RAM disponível', h('strong', fmt(state.value.ram_available_mib) + ' MiB'), h('small', 'Reserva: ' + fmt(state.value.ram_reserve_mib) + ' MiB')]),
          h('div', { class: 'rg-card' }, ['Previsão / perfil', h('strong', state.value.profile || 'Aprendendo'), h('small', state.value.forecast?.ready ? 'CPU prevista: ' + fmt(state.value.forecast.cpu_percent) + '%' : 'Aguardando histórico contínuo')])
        ]),
        state.value?.blocked > 0 && h('p', { class: 'rg-error' }, 'Ajustes automáticos bloqueados por ações não confirmadas. Consulte o registro e solicite restauração.'),
        state.value?.restart_required && h('p', { class: 'rg-notice' }, 'Diretório de dados alterado: reinicie o serviço para usar o novo local. O histórico antigo não será movido.'),
        h('div', [button('Atualizar', refresh), button('Pausar ajustes', () => action('pause')), button('Restaurar limites anteriores', () => action('restore'))]),
        h('small', 'Pausar mantém os limites já aplicados. Restaurar aguarda margem de RAM e respeita alterações externas.'),
        config.value && h('fieldset', { disabled: busy.value }, [
          h('legend', 'Configuração das VMs'),
          h('div', { class: 'rg-save' }, [
            h('b', busy.value ? 'Aguarde…' : canonical(config.value) === saved.value ? 'Configuração salva' : 'Alterações ainda não salvas'),
            button('Salvar configuração', () => action('configure', config.value))
          ]),
          h('h3', '1. Ativar gerenciamento'),
          h('label', ['Gerenciar automaticamente as VMs do MOS', h('input', {
            type: 'checkbox', checked: config.value.auto_vms, onChange: e => { config.value.auto_vms = e.target.checked; }
          })]),
          select('Como o Guardian deve agir?', config.value, 'mode', [['observe', 'Somente observar'], ['automatic', 'Aplicar automaticamente'], ['paused', 'Pausado']]),
          h('p', !config.value.auto_vms ? 'Gerenciamento desligado. Você pode consultar as VMs abaixo antes de ativar.' : config.value.mode !== 'automatic' ? 'As VMs serão monitoradas. Selecione Aplicar automaticamente para permitir ajustes e salve.' : 'Ao salvar, o Guardian poderá ajustar as VMs selecionadas, respeitando a reserva do servidor.'),
          h('h3', '2. Escolher as VMs'),
          button('Buscar VMs agora', discover),
          h('p', vms.value === null ? 'Buscando VMs…' : vms.value.length === 0 ? 'Nenhuma VM KVM persistente encontrada em qemu:///system. Verifique o serviço de VMs no MOS e clique em Buscar VMs agora.' : 'A descoberta funciona mesmo com o gerenciamento desligado. Os valores abaixo vêm da configuração salva no MOS.'),
          h('p', 'Crie a VM no MOS com a quantidade de CPU/RAM desejada e selecione os núcleos exigidos pelo editor. Ative CPU automática abaixo para liberar a afinidade das vCPUs em execução. A RAM dinâmica depende das estatísticas do balloon.'),
          h('div', (vms.value || []).map(vm => h('div', { class: vm.error ? 'rg-card rg-error' : 'rg-card', key: vm.uuid || 'discovery-error' }, [
            h('b', vm.name || vm.uuid || 'Descoberta'),
            h('p', vm.error || (vm.cpu_max + ' vCPUs • RAM até ' + fmt(vm.memory_max_mib / 1024) + ' GiB • mínimo automático ' + fmt(vm.memory_min_mib / 1024) + ' GiB')),
            h('small', vm.note || ''),
            vm.uuid && !vm.error && h('label', ['Gerenciar esta VM', h('input', { type: 'checkbox',
              checked: !config.value.auto_vm_exclude.includes(vm.uuid),
              onChange: e => { config.value.auto_vm_exclude = e.target.checked ? config.value.auto_vm_exclude.filter(x => x !== vm.uuid) : [...new Set([...config.value.auto_vm_exclude, vm.uuid])]; }
            })]),
            vm.uuid && !vm.error && h('label', ['CPU automática — liberar afinidade', h('input', { type: 'checkbox',
              checked: (config.value.free_affinity || []).includes(vm.uuid),
              onChange: e => { const ids = config.value.free_affinity || []; config.value.free_affinity = e.target.checked ? [...new Set([...ids, vm.uuid])] : ids.filter(x => x !== vm.uuid); }
            })]),
            vm.uuid && h('p', (state.value?.affinity || []).find(x => x.uuid === vm.uuid)?.message || 'Afinidade original até ativar e salvar em modo automático.')
          ]))),
          h('h3', '3. Salvar e acompanhar'),
          button('Salvar configuração', () => action('configure', config.value)),
          h('small', 'Salvar abaixo aplica a seleção. Desmarcar CPU automática restaura a afinidade original enquanto a VM estiver ligada. CPU/RAM: use Restaurar limites anteriores. Pausar mantém a afinidade aplicada.'),
          h('details', [h('summary', 'Opções avançadas: reserva do servidor e alvos manuais'),
          h('h3', 'Reserva do servidor'),
          h('div', { class: 'rg-grid' }, [
            select('Perfil', config.value, 'profile', [['automatic', 'Adaptativo'], ['balanced', 'Equilibrado'], ['conservative', 'Conservador']]),
            number('Reserva CPU (%)', config.value.monitor, 'cpu_reserve_percent', 1),
            number('Reserva RAM (MiB)', config.value.monitor, 'ram_reserve_mib', 1),
            number('Reserva RAM (%)', config.value.monitor, 'ram_reserve_percent', 1),
            number('Emergência RAM (MiB)', config.value.monitor, 'emergency_ram_mib', 1),
            number('Intervalo entre ajustes (s)', config.value, 'cooldown_seconds', 30),
            number('Prever próximos segundos', config.value, 'forecast_seconds', 10)
          ]),
          h('label', ['Diretório do histórico (use um disco persistente em /mnt)', h('input', { value: config.value.data_directory, onInput: e => { config.value.data_directory = e.target.value; } })]),
          h('h3', 'Alvos manuais (opcional)'),
          h('p', 'As VMs descobertas acima não precisam de cadastro aqui. Use para Docker/LXC ou uma política personalizada por VM. Prioridade 100 é a mais alta.'),
          ...config.value.targets.map((t, i) => h('fieldset', { key: i }, [h('legend', 'Alvo ' + (i+1)), h('div', { class: 'rg-grid' }, [
            select('Tipo', t, 'kind', [['vm', 'VM libvirt'], ['docker', 'Docker'], ['lxc', 'LXC']]),
            h('label', ['Nome ou ID', h('input', { required: true, value: t.id, onInput: e => { t.id = e.target.value; } })]),
            number('Prioridade', t, 'priority', 1), number('CPU mínima', t, 'cpu_min', .1, .1), number('CPU máxima', t, 'cpu_max', .1, .1),
            t.kind === 'vm' && select('Controle CPU', t, 'cpu_method', [['quota', 'Quota (recomendado)'], ['hotplug', 'Hotplug de vCPU']]),
            t.kind === 'lxc' && h('label', ['Diretório LXC (opcional)', h('input', { value: t.lxc_path || '', onInput: e => { if (e.target.value) t.lxc_path = e.target.value; else delete t.lxc_path; } })]),
            h('label', ['Ajustar memória', h('input', { type: 'checkbox', checked: t.manage_memory, onChange: e => { t.manage_memory = e.target.checked; } })]),
            t.manage_memory && number('RAM mínima (MiB)', t, 'memory_min_mib', 256),
            t.manage_memory && number('RAM máxima (MiB)', t, 'memory_max_mib', 256),
            t.manage_memory && number('Margem do guest (MiB)', t, 'memory_headroom_mib', 128)
          ]), button('Remover alvo', () => config.value.targets.splice(i, 1))])),
          button('Adicionar alvo', addTarget),
          button('Salvar opções avançadas', () => action('configure', config.value))
          ])
        ]),
        h('h3', 'Alvos e próximas decisões'),
        table(['Alvo', 'CPU', 'RAM (MiB)', 'Próximo ajuste', 'Diagnóstico'], (state.value?.targets || []).map(t => [t.target, fmt(t.cpu), fmt(t.memory_mib), t.proposed?.join(' → '), t.error || (t.memory_safe === false ? 'RAM sem estatísticas recentes ou não habilitada' : 'OK')])),
        h('h3', 'Registro de ações'),
        table(['Hora', 'Alvo', 'Recurso', 'Destino', 'Resultado'], (state.value?.actions || []).map(a => [new Date(a.timestamp*1000).toLocaleTimeString(), a.target, a.action.resource, JSON.stringify(a.action.after), a.error || a.status])),
        h('h3', 'Histórico recente'),
        table(['Hora', 'CPU (%)', 'RAM livre (MiB)', 'Estado'], history.value.slice(0, 20).map(s => [new Date(s.timestamp*1000).toLocaleTimeString(), fmt(s.cpu_percent), fmt(s.ram_available_mib), s.state]))
      ]);
    }
  };
}
