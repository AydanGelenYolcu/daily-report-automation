"""Windows Görev Zamanlayıcı kaydı (yönetici yetkisi gerekmez).

Görev "yalnız kullanıcı oturum açıkken" çalışır: tarayıcı otomasyonu açık
Windows oturumu ister. Bilgisayar kapalı/uykudaysa görev, açılınca bir kez çalışır.
"""
from __future__ import annotations

from datetime import date
import base64
import json
import os
import re
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

from .paths import APP_ROOT

DAY_TAGS = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
            "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}
NO_WINDOW = 0x08000000


def task_name(profile: dict) -> str:
    label = profile.get('schedule',{}).get('name') or f"{profile.get('name',profile['id'])} günlük raporu"
    return f"Netmera - {label} ({profile['id']})"


def task_owner(profile: dict) -> str:
    return f"urn:netmera-otomasyonu:profile:{profile['id']}"


def _arguments(profile: dict) -> str:
    return f'-m netmera_automation run --profile {profile["id"]} --zamanli'


def python_for_tasks() -> Path:
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return windowless if windowless.is_file() else exe


def build_task_xml(profile: dict, python_exe: Path, app_root: Path = APP_ROOT, start: date | None = None) -> str:
    schedule = profile["schedule"]
    start = start or date.today()
    days = "".join(f"<{DAY_TAGS[d]} />" for d in schedule["days"])
    triggers = "".join(
        f"""
    <CalendarTrigger>
      <StartBoundary>{start.isoformat()}T{t}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>
    </CalendarTrigger>""" for t in schedule["times"])
    user = escape(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\"))
    arguments = escape(f'-m netmera_automation run --profile {profile["id"]} --zamanli')
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>{escape(task_owner(profile))}</URI>
    <Description>{escape(f"Netmera Otomasyonu: {profile['name']} profilinin etkin işlerini çalıştırır ve Excel'e yazar.")}</Description>
  </RegistrationInfo>
  <Triggers>{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python_exe))}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{escape(str(app_root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="oem",
                          errors="replace", creationflags=NO_WINDOW if os.name == "nt" else 0)



def _powershell(script: str) -> dict:
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    exe = Path(os.environ.get('SystemRoot',r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    result = subprocess.run([str(exe),'-NoProfile','-NonInteractive','-WindowStyle','Hidden','-EncodedCommand',encoded],
                            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15,
                            creationflags=NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('Windows görev kayıtları okunamadı; işlem yapılmadı.')
    value = json.loads(result.stdout.lstrip('\ufeff').strip())
    if not isinstance(value,dict) or not isinstance(value.get('tasks'),list):
        raise ValueError('Windows görev yanıtı geçersiz.')
    return value


def _inventory(profile: dict) -> list[dict]:
    # Yalnız bu projeyle ilişkili adaylar geri döner. Başka kullanıcı görevleri
    # silinmez; SID eşleşmesi mutasyondan önce Python tarafında da zorunludur.
    literal = lambda value: "'" + value.replace("'","''") + "'"
    script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function shownDate($d) { if ($d.Year -ge 2000) { $d.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null } }
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$out = @()
foreach ($task in $service.GetFolder('\').GetTasks(1)) {
  try { $def = $task.Definition } catch {
    if ($task.Name -eq __NAME__ -or $task.Name -eq __LEGACY__) { throw }
    continue
  }
  $uri = [string]$def.RegistrationInfo.URI
  $action = $null
  if ($def.Actions.Count -eq 1 -and $def.Actions.Item(1).Type -eq 0) { $action = $def.Actions.Item(1) }
  $taskArguments = if ($null -ne $action) { [string]$action.Arguments } else { '' }
  if ($uri -ne __OWNER__ -and $task.Name -ne __NAME__ -and $task.Name -ne __LEGACY__ -and $taskArguments -ne __ARGS__) { continue }
  $sameOwner = $false
  try {
    $account = [string]$def.Principal.UserId
    $sid = if ($account.StartsWith('S-1-')) { $account } else { (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value }
    $sameOwner = $sid -eq $me
  } catch { $sameOwner = $false }
  $out += @{name=[string]$task.Name; owner_matches=[bool]$sameOwner; xml=[string]$task.Xml;
    state=[int]$task.State; enabled=[bool]$task.Enabled; last_run=(shownDate $task.LastRunTime);
    next_run=(shownDate $task.NextRunTime); result_code=[long]$task.LastTaskResult;
    command=if ($action) { [string]$action.Path } else { '' };
    working_directory=if ($action) { [string]$action.WorkingDirectory } else { '' }; arguments=$taskArguments}
}
@{tasks=@($out)} | ConvertTo-Json -Depth 4 -Compress
"""
    values={'__OWNER__':task_owner(profile),'__NAME__':task_name(profile),
            '__LEGACY__':f"Netmera Otomasyonu - {profile['id']}",'__ARGS__':_arguments(profile)}
    script=re.sub(r'__(?:OWNER|NAME|LEGACY|ARGS)__',lambda match:literal(values[match.group()]),script)
    return _powershell(script)['tasks']


def _owned(task: dict, profile: dict) -> bool:
    if task.get('owner_matches') is not True:
        return False
    ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    try:
        root=ET.fromstring(task['xml'])
        actions=root.find('t:Actions',ns)
        if actions is None or len(actions) != 1 or actions[0].tag != '{'+ns['t']+'}Exec':
            return False
        command=actions[0].findtext('t:Command',default='',namespaces=ns)
        arguments=actions[0].findtext('t:Arguments',default='',namespaces=ns)
        uri=root.findtext('t:RegistrationInfo/t:URI',default='',namespaces=ns)
        allowed_uris=('',task_owner(profile),'\\'+task['name'],task['name'],f"\\Netmera Otomasyonu - {profile['id']}")
        return (PureWindowsPath(command).name.casefold() in ('python.exe','pythonw.exe')
                and arguments.split() == _arguments(profile).split() and uri in allowed_uris)
    except (KeyError,TypeError,ET.ParseError):
        return False


def _path_matches(task: dict, profile: dict) -> bool:
    norm=lambda v:os.path.normcase(str(Path(v).resolve()))
    return (norm(task.get('command','')) == norm(python_for_tasks())
            and norm(task.get('working_directory','')) == norm(APP_ROOT)
            and task.get('arguments','').split() == _arguments(profile).split())


def _fresh_snapshot(profile: dict, original: dict) -> dict:
    fresh=next((t for t in _inventory(profile) if t['name'].casefold()==original['name'].casefold()),None)
    if (not fresh or not _owned(fresh,profile) or fresh.get('xml') != original.get('xml')
            or fresh.get('state') in (2,4)):
        raise ValueError('Görev kaydı değişti veya çalışmaya başladı. Durumu yenileyip yeniden deneyin; görev silinmedi.')
    return fresh


def _register(name: str, xml: str, *, overwrite: bool=False) -> None:
    with tempfile.TemporaryDirectory(prefix='netmera-gorev-') as folder:
        path=Path(folder)/'gorev.xml'; path.write_text(xml,encoding='utf-16')
        args=['/Create','/TN',name,'/XML',str(path)]
        if overwrite: args.append('/F')
        result=_schtasks(*args)
    if result.returncode:
        raise RuntimeError('Windows görev kaydı oluşturulamadı. Yetki veya kurum politikasını kontrol edin.')


def _delete(profile: dict, snapshot: dict) -> None:
    _fresh_snapshot(profile,snapshot)  # Değişmiş/başka kullanıcıya ait kayda dokunma.
    if _schtasks('/Delete','/TN',snapshot['name'],'/F').returncode:
        raise RuntimeError('Windows görev kaydı silinemedi. Temizlik tamamlanmadı.')


def _mutation(profile: dict, operation: str) -> dict:
    from . import store
    if operation != 'remove' and not profile['schedule']['times']:
        raise ValueError('Önce en az bir saat ekleyin.')
    with store.profile_lock(profile['id']):
        candidates=_inventory(profile)
        owned=[t for t in candidates if _owned(t,profile)]
        wanted=task_name(profile)
        collision=next((t for t in candidates if t['name'].casefold()==wanted.casefold() and not _owned(t,profile)),None)
        if collision and operation != 'remove':
            raise ValueError('Bu görev adı başka bir kayda ait. Farklı bir görev adı yazın; mevcut kayıt değiştirilmedi.')
        if any(t.get('state') in (2,4) for t in owned):
            raise ValueError('Bu projenin bir görevi çalışıyor veya sırada. Bitince yeniden deneyin.')
        if operation == 'install' and any(t['name'].casefold()!=wanted.casefold() for t in owned):
            raise ValueError('Eski adla görev kaydı var. Temizle ve baştan oluştur düğmesini kullanın.')
        snapshots=[_fresh_snapshot(profile,t) for t in owned]
        new_xml=build_task_xml(profile,python_for_tasks()) if operation!='remove' else None
        removed=[]
        updated=None
        created=False
        try:
            if operation in ('remove','reset'):
                for t in snapshots:
                    _delete(profile,t); removed.append(t)
            if operation == 'install' and snapshots:
                updated=snapshots[0]
                _fresh_snapshot(profile,updated)
                _register(wanted,new_xml,overwrite=True)
            elif operation != 'remove':
                _register(wanted,new_xml); created=True
            after=_inventory(profile)
            remaining=[t for t in after if _owned(t,profile)]
            if operation == 'remove':
                if remaining: raise RuntimeError('Silme sonrası görev kaydı hâlâ görünüyor.')
            else:
                if (len(remaining)!=1 or remaining[0]['name'].casefold()!=wanted.casefold()
                        or not _path_matches(remaining[0],profile)):
                    raise RuntimeError('Kurulum sonrası görev adı/yolu doğrulanamadı.')
            return {'task':wanted,'removed':len(removed),'installed':operation!='remove',
                    'skipped':len(candidates)-len(owned)}
        except Exception as exc:
            rollback_errors=[]
            if created:
                try:
                    current=next((t for t in _inventory(profile) if t['name'].casefold()==wanted.casefold()),None)
                    if current and _owned(current,profile): _delete(profile,current)
                    elif current: raise RuntimeError('Yeni kayıt değişmiş')
                except Exception: rollback_errors.append(wanted)
            if updated:
                try:
                    current=next((t for t in _inventory(profile) if t['name'].casefold()==updated['name'].casefold()),None)
                    if current: _fresh_snapshot(profile,current)
                    _register(updated['name'],updated['xml'],overwrite=bool(current))
                except Exception: rollback_errors.append(updated['name'])
            for old in removed:
                try: _register(old['name'],old['xml'])
                except Exception: rollback_errors.append(old['name'])
            suffix=(' Önceki görevlerin tamamı geri yüklenemedi; durumu kontrol edin.' if rollback_errors
                    else ' Önceki görev kayıtları korundu veya geri yüklendi.')
            raise RuntimeError(str(exc)+suffix) from exc


def install(profile: dict) -> str:
    return _mutation(profile,'install')['task']


def reset(profile: dict) -> dict:
    return _mutation(profile,'reset')


def remove(profile: dict) -> bool:
    return _mutation(profile,'remove')['removed'] > 0


def is_installed(profile: dict) -> bool:
    return status(profile).get('installed') is True


def status(profile: dict) -> dict:
    try:
        tasks=_inventory(profile)
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
        return {'installed':None,'task':task_name(profile),'error':'Windows görev durumu okunamadı. Yetki veya Görev Zamanlayıcı erişimini kontrol edin.'}
    owned=[t for t in tasks if _owned(t,profile)]
    desired=task_name(profile)
    info={'installed':bool(owned),'task':desired,'desired_task':desired,'related_tasks':[],
          'conflicts':[t['name'] for t in tasks if not _owned(t,profile)]}
    for t in owned:
        info['related_tasks'].append({'name':t['name'],'state':t.get('state'),'path_matches':_path_matches(t,profile)})
    if owned:
        chosen=next((t for t in owned if t['name'].casefold()==desired.casefold()),owned[0])
        info.update({k:chosen.get(k) for k in ('state','enabled','last_run','next_run','result_code')})
        info.update(task=chosen['name'],path_matches=_path_matches(chosen,profile),
                    result_hex=f"0x{int(chosen.get('result_code',0)) & 0xffffffff:08X}",
                    needs_cleanup=len(owned)>1 or chosen['name'].casefold()!=desired.casefold())
    return info


def run_now(profile: dict) -> None:
    info=status(profile)
    if (not info.get('installed') or not info.get('enabled') or not info.get('path_matches')
            or info.get('needs_cleanup')):
        raise ValueError('Görev hazır değil veya eski ad/klasör kullanıyor. Temizle ve baştan oluştur düğmesini kullanın.')
    if info.get('state') in (2,4):
        raise ValueError('Görev zaten çalışıyor veya sırada.')
    result=_schtasks('/Run','/TN',info.get('task',task_name(profile)))
    if result.returncode:
        raise RuntimeError('Windows görevi başlatamadı. Görev erişimini kontrol edin.')
