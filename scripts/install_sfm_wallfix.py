"""Build JuPedSim 1.4.2 with corrected wall friction and stable contact integration.

The native source stays LGPL-3.0-or-later. Correct obstacle relative velocity
and integrate contact friction semi-implicitly; force coefficients stay intact.
"""
import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://github.com/PedestrianDynamics/jupedsim/archive/refs/tags/v1.4.2.tar.gz'
SHA256 = 'a59c6bbd388cfd572fc8eb3b276f0cf41fdbc5c813b795a5a408b3f5f4950dc3'
PATCHED_VERSION = '1.4.2+crowdsim.sfmfix2'


def patch_sources(root):
    cpp = root / 'libsimulator/src/SocialForceModel.cpp'
    original = 'agent.pos, pt, model.obstacleScale, model.forceDistance, model.radius, model.velocity);'
    text = cpp.read_text()
    if text.count(original) != 1:
        raise ValueError('Unexpected upstream SFM source; refusing to apply patch')
    text = text.replace(original, original.replace('model.velocity);', '-model.velocity);'))
    # The explicit contact damping becomes unstable when dt*kappa*overlap/m
    # exceeds its stability bound. Solve only the local velocity-dependent
    # friction implicitly. Neighbour velocities and normal forces stay at t_n.
    before = '    Point F_rep;'
    after = '''    Point F_rep;
    double damping_xx = 0., damping_xy = 0., damping_yy = 0.;
    const auto add_contact_damping = [&](const Point separation, const double radius) {
        const double overlap = radius - separation.Norm();
        if(overlap <= 0.) return;
        const Point tangent = separation.Normalized().Rotate90Deg();
        const double coefficient = friction * overlap / model.mass;
        damping_xx += coefficient * tangent.x * tangent.x;
        damping_xy += coefficient * tangent.x * tangent.y;
        damping_yy += coefficient * tangent.y * tangent.y;
    };'''
    assert text.count(before) == 1
    text = text.replace(before, after)
    before = '        F_rep += AgentForce(ped, neighbor);'
    assert text.count(before) == 1
    text = text.replace(before, before + '''
        add_contact_damping(ped.pos - neighbor.pos,
            model.radius + std::get<SocialForceModelData>(neighbor.model).radius);''')
    before = '    update.velocity = model.velocity + forces * dT;'
    after = '''    for(const auto& wall : walls) {
        add_contact_damping(ped.pos - wall.ShortestPoint(ped.pos), model.radius);
    }
    // IMEX Euler: (I + dt*D) v[n+1] = v[n] + dt*(a[n] + D*v[n]).
    // D is positive semidefinite; wall friction cannot amplify slip velocity.
    const Point rhs = model.velocity + forces * dT + Point(
        damping_xx * model.velocity.x + damping_xy * model.velocity.y,
        damping_xy * model.velocity.x + damping_yy * model.velocity.y) * dT;
    const double a = 1. + dT * damping_xx;
    const double b = dT * damping_xy;
    const double c = 1. + dT * damping_yy;
    const double determinant = a * c - b * b;
    update.velocity = Point((c * rhs.x - b * rhs.y) / determinant,
                            (a * rhs.y - b * rhs.x) / determinant);'''
    assert text.count(before) == 1
    cpp.write_text(text.replace(before, after))
    setup = root / 'setup.py'
    text = setup.read_text()
    if text.count('version=version,') != 1 or text.count('build_args += ["-j"]') != 1:
        raise ValueError('Unexpected upstream build metadata')
    # Identify the backport in installed metadata; bound compiler parallelism.
    setup.write_text(text.replace('version=version,', 'version=version + "+crowdsim.sfmfix2",')
                     .replace('build_args += ["-j"]', 'build_args += ["-j2"]'))


def verify(env):
    subprocess.run([sys.executable, '-c',
                    'from data_service.prediction.sfm_engine import verified_sfm_engine; print(verified_sfm_engine())'],
                   cwd=ROOT, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Already downloaded official v1.4.2 archive')
    parser.add_argument('--direct-network', action='store_true', help='Ignore proxy variables for package build downloads')
    args = parser.parse_args()
    env = dict(os.environ)
    if args.direct_network:
        for key in ('ALL_PROXY', 'all_proxy', 'HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy'):
            env.pop(key, None)
    try:
        if version('jupedsim') == PATCHED_VERSION:
            verify(env)
            print('社会力墙体摩擦修复已安装并通过校验。')
            return
    except PackageNotFoundError:
        pass
    with tempfile.TemporaryDirectory(prefix='crowdsim-sfm-build-') as directory:
        work = Path(directory)
        archive = args.archive or work / 'source.tar.gz'
        if not args.archive:
            subprocess.run(['curl', '--fail', '--location', URL, '--output', str(archive)], env=env, check=True)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise ValueError('Official source checksum mismatch; nothing installed')
        with tarfile.open(archive) as source:
            source.extractall(work, filter='data')
        root = work / 'jupedsim-1.4.2'
        patch_sources(root)
        wheels = work / 'wheels'
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', str(root), '-w', str(wheels)], env=env, check=True)
        wheel, = wheels.glob('jupedsim-*.whl')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--force-reinstall', str(wheel)], env=env, check=True)
    # A fresh process must load the newly installed native library.
    verify(env)
    print('修复完成；请重启数据服务后重新运行，旧实验结果保持原样。')


if __name__ == '__main__':
    main()
