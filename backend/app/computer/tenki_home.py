"""Guest-only home migration. Keep durable user files when a VM is replaced."""

HOME_SCRIPT = r'''
import filecmp,json,os,pathlib,shutil,uuid
root=pathlib.Path(root)
home=pathlib.Path(home)
assert os.path.ismount(root), 'Persistent disk missing'
marker=root/'.band-owner'
assert not marker.exists() or marker.read_text()==expected_owner, 'Disk owner mismatch'
marker.write_text(expected_owner)
saved=root/'.home'
saved.mkdir(exist_ok=True)
conflicts=root/'.band-home-conflicts'/uuid.uuid4().hex

def exists(path):
 return os.path.lexists(path)

def merge(source,target):
 # A replacement image must never overwrite the user's saved configuration.
 if not exists(target):
  target.parent.mkdir(parents=True,exist_ok=True)
  if source.is_dir() and not source.is_symlink():
   target.mkdir()
   for child in source.iterdir(): merge(child,target/child.name)
   shutil.copystat(source,target)
   source.rmdir()
  else:
   # Copy across filesystems before removing the original. A failed copy must
   # not leave a partial file in the active durable location.
   staged=target.with_name('.band-home-'+uuid.uuid4().hex)
   try:
    if source.is_symlink():
     link=os.readlink(source)
     staged.symlink_to(link if os.path.isabs(link) else str(source.parent/link),target_is_directory=source.is_dir())
    else: shutil.copy2(source,staged)
    os.replace(staged,target)
    source.unlink()
   finally:
    if exists(staged): staged.unlink()
 elif source.is_dir() and target.is_dir() and not source.is_symlink() and not target.is_symlink():
  for child in source.iterdir(): merge(child,target/child.name)
  source.rmdir()
 elif source.is_file() and target.is_file() and not source.is_symlink() and not target.is_symlink() and filecmp.cmp(source,target,shallow=False):
  source.unlink()
 else:
  # Preserve both versions of a conflict; the durable version remains active.
  backup=conflicts/source.relative_to(home)
  backup.parent.mkdir(parents=True,exist_ok=True)
  merge(source,backup)

legacy={'.config','.cache','.local'}
folders=legacy|{'Desktop','Documents','Downloads','Pictures','Music','Videos','Public','Templates'}
names={p.name for p in home.iterdir()}|{p.name for p in saved.iterdir()}|folders
for name in sorted(names):
 # Tenki's readiness probe writes .tenki-readiness and rejects symlink paths.
 # Provider control files and our per-VM gateway/transfer files stay local.
 if name=='workspace' or name=='band-observation.png' or name.startswith(('.band-','.tenki')): continue
 source=home/name
 target=(root if name in legacy else saved)/name
 if source.is_symlink() and source.resolve()==target.resolve(): continue
 if exists(source): merge(source,target)
 if not exists(target): target.mkdir()
 source.symlink_to(target,target_is_directory=target.is_dir())
 assert source.resolve()==target.resolve()

vm_marker=root/'.band-vm'
if not vm_marker.exists() or vm_marker.read_text()!=vm_id:
 profile=root/'.config/band-browser'
 for name in ['SingletonLock','SingletonSocket','SingletonCookie']:
  try: (profile/name).unlink()
  except FileNotFoundError: pass
 vm_marker.write_text(vm_id)
'''


def home_script(*, owner: str, vm_id: str, home: str, root: str) -> str:
    return (f"expected_owner={owner!r}\nvm_id={vm_id!r}\nhome={home!r}\nroot={root!r}\n"
            + HOME_SCRIPT)
