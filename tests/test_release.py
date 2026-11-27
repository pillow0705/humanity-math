import io,tarfile
from pathlib import Path
from unittest.mock import patch
import pytest
from deploy.release import unpack_release,restore_previous


def archive(path,name,kind=None):
    with tarfile.open(path,'w:gz') as tar:
        item=tarfile.TarInfo(name);item.size=1
        if kind:item.type=kind;item.linkname='/etc/passwd';item.size=0
        tar.addfile(item,None if kind else io.BytesIO(b'x'))


@pytest.mark.parametrize('name,kind',[('../escape',None),('/etc/passwd',None),('data/credentials.json',None),('tools/toolchain',None),('apps/symlink',tarfile.SYMTYPE),('apps/hardlink',tarfile.LNKTYPE)])
def test_release_rejects_paths_and_links(tmp_path,name,kind):
    source=tmp_path/'input.tgz';archive(source,name,kind)
    with pytest.raises(ValueError):unpack_release(source,tmp_path/'release')
    assert not (tmp_path/'escape').exists()


def test_release_accepts_code_and_rollback_restores_pointer_and_environment(tmp_path):
    source=tmp_path/'input.tgz'
    with tarfile.open(source,'w:gz') as tar:
        for name in ['apps/api/main.py','requirements.txt']:
            info=tarfile.TarInfo(name);info.size=1;tar.addfile(info,io.BytesIO(b'x'))
    new=tmp_path/'new';new.mkdir();unpack_release(source,new)
    old=tmp_path/'old';old.mkdir();current=tmp_path/'current';current.symlink_to(new,target_is_directory=True)
    env=tmp_path/'runtime.env';env.write_text('APP_RELEASE=new\n')
    with patch('deploy.release.run') as run,patch('deploy.release.health') as health:
        restore_previous(current,old,env,'APP_RELEASE=old\n','old')
    assert current.resolve()==old and env.read_text()=='APP_RELEASE=old\n'
    health.assert_called_once_with('old')
    assert run.call_count==2
