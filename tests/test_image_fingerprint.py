#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线镜像指纹兼容性验证（独立脚本，不进入应用包）。

针对 Docker 29（containerd 镜像存储）下 `docker inspect .Id` 与旧版
`docker save` 的 `manifest.json` `Config` 不再一致的问题，验证：

    A. `fnos-app/cmd/main` 里的镜像指纹推导 / 匹配逻辑：
       1. 经典包：Config 裸 hash、Config <64hex>.json
       2. OCI 包：Config 为 blobs/sha256/<...> 时，Config 与 index.json 顶层
          manifests 描述符的 digest 都作为候选，双方都能命中
       3. 旧版单行指纹文件在 OCI 镜像上不匹配时，从包内刷新一次即可命中
       4. 无关当前镜像被拒绝
       5. 缺 index.json 的包照常处理
       6. 非法 Config / digest 不被接受
       7. 至少覆盖一个 .tar.gz 包
    B. main 底部只读 `image-ids` 子命令：从包内输出候选，且不调用 docker
    C. `container_image_matches`：两枚候选都命中，无关 ID 被拒绝
    D. `fnos-app/cmd/install_callback`：候选集（多行）下的跳过 / 导入后复核，
       经典与 OCI 各命中一次即跳过，不触发 rmi/load

实现方式（纯隔离，不做任何真实操作）：
  - 把 `cmd/main` 中「底部 case 分发」整段去掉后 source 到临时文件，
    保留 container_image_matches 等全部函数定义；source 过程不执行
    start/stop/ensure_image。
  - PATH 前置一个只记录命令、按环境变量返回的假 docker，绝不执行真实 docker。
  - install_callback 用测试临时 TRIM 目录 + 小 tar.gz fixture 真实调用一次。
  - 工作目录走 `tempfile`（Docker 运行时通过 TMPDIR=/execution/... 指定）。

用法：
    python3 tests/test_image_fingerprint.py

退出码：0 全部通过；1 有失败；2 未运行（读不到 --source）。
本脚本设计为在目标 Docker 环境里运行，不应在本机执行。
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

# 四个测试用摘要（合法 64 位小写十六进制）
H_A = "a" * 64
H_B = "b" * 64
H_C = "c" * 64
H_D = "d" * 64


def default_source():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "fnos-app", "cmd", "main"))


def extract_prefix(source_text):
    """返回 source_text 中「底部 case 分发」之前的全部内容。

    只去掉脚本末尾的 case 分发块，保留 container_image_matches 等所有函数定义
    （它们在 ensure_image 之后）。source 时不触发 start/stop/ensure_image。
    """
    lines = source_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == 'case "$1" in':
            return "".join(lines[:i])
    raise AssertionError("未能在源码中定位底部 case 分发")


def make_image_tar(path, members, gz=False):
    """构造一个最小 docker save 产物 tar（成员名 -> 文本内容）。"""
    mode = "w:gz" if gz else "w"
    with tarfile.open(path, mode) as tf:
        for name, data in members.items():
            blob = data.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(blob))


def manifest_json(config):
    return json.dumps([{
        "Config": config,
        "RepoTags": ["maa-fnos:local"],
        "Layers": ["blobs/sha256/" + "0" * 64],
    }])


def index_json(digests):
    return json.dumps({
        "schemaVersion": 2,
        "manifests": [{
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "digest": d,
            "size": 1234,
            "platform": {"architecture": "amd64", "os": "linux"},
        } for d in digests],
    })


# 只记录命令、按环境变量返回的假 docker；绝不执行任何真实 Docker 操作。
#   FAKE_DOCKER_EXISTS            image inspect 是否命中（默认 1）
#   FAKE_DOCKER_ID                `image inspect --format {{.Id}}` 的返回值
#   FAKE_DOCKER_ID_AFTER          load 之后 `image inspect --format {{.Id}}` 的返回值
#   FAKE_DOCKER_LOADED_MARK       load 成功时写出的标记文件（用于切换 AFTER 状态）
#   FAKE_DOCKER_CONTAINER_EXISTS  container inspect 是否命中（默认 1）
#   FAKE_CONTAINER_IMAGE          `inspect --format {{.Image}}` 的返回值
#   FAKE_DOCKER_LOG               记录每次调用的文件（未设置则完全不写）
FAKE_DOCKER = """#!/bin/sh
[ -z "${FAKE_DOCKER_LOG}" ] || echo "$*" >> "${FAKE_DOCKER_LOG}"

current_image_id() {
    if [ -n "${FAKE_DOCKER_LOADED_MARK}" ] && [ -f "${FAKE_DOCKER_LOADED_MARK}" ] \
            && [ -n "${FAKE_DOCKER_ID_AFTER}" ]; then
        printf '%s\\n' "${FAKE_DOCKER_ID_AFTER}"
    else
        printf '%s\\n' "${FAKE_DOCKER_ID}"
    fi
}

case "$1" in
    image)
        [ "$2" = "inspect" ] || exit 0
        [ "${FAKE_DOCKER_EXISTS:-1}" = "1" ] || exit 1
        shift 2
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --format=*)
                    case "${1#--format=}" in *Id*) current_image_id ;; esac
                    shift ;;
                --format)
                    case "$2" in *Id*) current_image_id ;; esac
                    shift 2 ;;
                *) shift ;;
            esac
        done
        exit 0
        ;;
    inspect)
        [ "${FAKE_DOCKER_CONTAINER_EXISTS:-1}" = "1" ] || exit 1
        shift
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --format=*)
                    case "${1#--format=}" in
                        *Image*) [ -n "${FAKE_CONTAINER_IMAGE}" ] && printf '%s\\n' "${FAKE_CONTAINER_IMAGE}" ;;
                    esac
                    shift ;;
                --format)
                    case "$2" in
                        *Image*) [ -n "${FAKE_CONTAINER_IMAGE}" ] && printf '%s\\n' "${FAKE_CONTAINER_IMAGE}" ;;
                    esac
                    shift 2 ;;
                *) shift ;;
            esac
        done
        exit 0
        ;;
    load)
        [ -z "${FAKE_DOCKER_LOADED_MARK}" ] || : > "${FAKE_DOCKER_LOADED_MARK}"
        exit "${FAKE_DOCKER_LOAD_RC:-0}"
        ;;
    rmi)
        exit 0
        ;;
esac
exit 0
"""


class ImageFingerprintTests(unittest.TestCase):
    PREFIX = None            # 由 main() 注入
    MAIN = None              # cmd/main 真实路径
    INSTALL_CALLBACK = None  # 同目录的 install_callback 真实路径

    def setUp(self):
        base = tempfile.mkdtemp(prefix="maa-fingerprint-")  # 遵循 TMPDIR
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.base = base
        self.pkgvar = os.path.join(base, "pkvar")
        self.appdest = os.path.join(base, "appdest")
        self.bin = os.path.join(base, "bin")
        for d in (self.pkgvar, self.appdest, self.bin):
            os.makedirs(d)
        self.temp_log = os.path.join(base, "temp.log")

        docker = os.path.join(self.bin, "docker")
        with open(docker, "w", encoding="utf-8") as fh:
            fh.write(FAKE_DOCKER)
        os.chmod(docker, 0o755)

        self.prefix_file = os.path.join(base, "main_prefix.sh")
        with open(self.prefix_file, "w", encoding="utf-8") as fh:
            fh.write(self.PREFIX)

    def _env(self, docker_id=None, docker_exists=True, container_image=None):
        env = os.environ.copy()
        env["TRIM_PKGVAR"] = self.pkgvar
        env["TRIM_APPDEST"] = self.appdest
        env["TRIM_TEMP_LOGFILE"] = self.temp_log
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["FAKE_DOCKER_ID"] = docker_id or ""
        env["FAKE_DOCKER_EXISTS"] = "1" if docker_exists else "0"
        env["FAKE_CONTAINER_IMAGE"] = container_image or ""
        env["FAKE_DOCKER_CONTAINER_EXISTS"] = "1"
        return env

    def run_bash(self, body, docker_id=None, docker_exists=True, container_image=None):
        script = '. "%s"\n%s\n' % (self.prefix_file, body)
        return subprocess.run(
            ["bash", "-c", script],
            env=self._env(docker_id=docker_id, docker_exists=docker_exists,
                          container_image=container_image),
            capture_output=True, text=True,
        )

    def build_package(self, members, gz=False):
        name = "maa-image.tar.gz" if gz else "maa-image.tar"
        path = os.path.join(self.appdest, name)
        make_image_tar(path, members, gz=gz)
        return path

    def derive(self, pkg):
        res = self.run_bash('derive_image_id "%s"; echo "RC=$?"' % pkg)
        lines = res.stdout.strip().splitlines()
        self.assertTrue(lines and lines[-1].startswith("RC="), res.stdout)
        return lines[:-1], lines[-1]

    def image_ready_rc(self, docker_id):
        res = self.run_bash('image_ready; echo "RC=$?"', docker_id=docker_id)
        lines = res.stdout.strip().splitlines()
        self.assertTrue(lines, res.stdout)
        return lines[-1]

    def container_match_rc(self, container_image):
        res = self.run_bash('container_image_matches; echo "RC=$?"',
                            container_image=container_image)
        lines = res.stdout.strip().splitlines()
        self.assertTrue(lines, res.stdout)
        return lines[-1]

    def run_main_image_ids(self, docker_log):
        env = self._env()
        env["FAKE_DOCKER_LOG"] = docker_log
        return subprocess.run(["bash", self.MAIN, "image-ids"], env=env,
                              capture_output=True, text=True)

    def run_install(self, docker_id, docker_log, id_after=None):
        env = self._env(docker_id=docker_id)
        env["FAKE_DOCKER_LOG"] = docker_log
        if id_after is not None:
            env["FAKE_DOCKER_ID_AFTER"] = id_after
            env["FAKE_DOCKER_LOADED_MARK"] = os.path.join(self.base, "loaded.mark")
        return subprocess.run(["bash", self.INSTALL_CALLBACK], env=env,
                              capture_output=True, text=True)

    def read_commands(self, path):
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def install_log_text(self):
        path = os.path.join(self.pkgvar, "install.log")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    # --- 经典格式 ---------------------------------------------------------

    def test_classic_bare_hash(self):
        pkg = self.build_package({"manifest.json": manifest_json(H_A)})
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_A])
        self.assertEqual(rc, "RC=0")

    def test_classic_hash_json_suffix_stripped(self):
        pkg = self.build_package({"manifest.json": manifest_json(H_A + ".json")})
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_A])
        self.assertEqual(rc, "RC=0")

    def test_classic_without_index_json(self):
        # 缺 index.json 的经典包正常处理（不带路径形式的 Config）
        pkg = self.build_package({"manifest.json": manifest_json(H_A)})
        out, rc = self.derive(pkg)
        self.assertNotIn("index", out)
        self.assertEqual(out, ["sha256:" + H_A])
        self.assertEqual(rc, "RC=0")

    def test_gz_package_is_handled(self):
        pkg = self.build_package({"manifest.json": manifest_json(H_A)}, gz=True)
        self.assertTrue(pkg.endswith(".tar.gz"))
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_A])
        self.assertEqual(rc, "RC=0")

    # --- OCI / containerd 格式 -------------------------------------------

    def test_oci_config_and_index_both_match(self):
        pkg = self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_B]),
        })
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_A, "sha256:" + H_B])
        self.assertEqual(rc, "RC=0")
        # 当前 .Id = Config 推导值 → 命中
        self.assertEqual(self.image_ready_rc("sha256:" + H_A), "RC=0")
        # 当前 .Id = index 顶层描述符 digest（Docker 29 真实情况）→ 命中
        self.assertEqual(self.image_ready_rc("sha256:" + H_B), "RC=0")

    def test_oci_config_and_index_dedup(self):
        pkg = self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_A]),
        })
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_A])
        self.assertEqual(rc, "RC=0")

    def test_oci_path_config_without_index(self):
        # 路径形式 Config 但缺 index：仍以 Config 推导值作为候选
        pkg = self.build_package({"manifest.json": manifest_json("blobs/sha256/" + H_A)})
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_A])
        self.assertEqual(rc, "RC=0")

    def test_stale_single_line_fingerprint_refreshed(self):
        # 旧版指纹文件只存了 Config 推导值；真实 .Id 是 index digest
        self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_B]),
        })
        id_file = os.path.join(self.appdest, "maa-image.id")
        with open(id_file, "w", encoding="utf-8") as fh:
            fh.write("sha256:%s\n" % H_A)

        self.assertEqual(self.image_ready_rc("sha256:" + H_B), "RC=0")
        # 刷新后应写入完整候选集，含真实镜像 ID
        with open(id_file, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("sha256:" + H_B, content)

    # --- 拒绝与非法输入 ---------------------------------------------------

    def test_unrelated_current_image_rejected(self):
        self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_B]),
        })
        self.assertEqual(self.image_ready_rc("sha256:" + H_C), "RC=1")

    def test_invalid_config_rejected(self):
        pkg = self.build_package({"manifest.json": manifest_json("deadbeef.json")})
        out, rc = self.derive(pkg)
        self.assertEqual(out, [])
        self.assertEqual(rc, "RC=1")

    def test_invalid_index_digest_ignored(self):
        pkg = self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:not-a-hex", "sha256:" + H_D[:63]]),
        })
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_A])
        self.assertEqual(rc, "RC=0")

    def test_invalid_config_with_valid_index(self):
        pkg = self.build_package({
            "manifest.json": manifest_json("blobs/sha256/notahash"),
            "index.json": index_json(["sha256:" + H_B]),
        })
        out, rc = self.derive(pkg)
        self.assertEqual(out, ["sha256:" + H_B])
        self.assertEqual(rc, "RC=0")

    # --- main 只读 image-ids 子命令 ---------------------------------------

    def test_main_image_ids_classic_readonly(self):
        self.build_package({"manifest.json": manifest_json(H_A)}, gz=True)
        log = os.path.join(self.base, "docker.log")
        res = self.run_main_image_ids(log)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip().splitlines(), ["sha256:" + H_A])
        self.assertFalse(os.path.exists(log), "image-ids 不应调用 docker")

    def test_main_image_ids_oci_readonly(self):
        self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_B]),
        }, gz=True)
        log = os.path.join(self.base, "docker.log")
        res = self.run_main_image_ids(log)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip().splitlines(),
                         ["sha256:" + H_A, "sha256:" + H_B])
        self.assertFalse(os.path.exists(log), "image-ids 不应调用 docker")

    # --- container_image_matches 多候选 -----------------------------------

    def test_container_image_matches_both_candidates(self):
        self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_B]),
        })
        self.assertEqual(self.container_match_rc("sha256:" + H_A), "RC=0")
        self.assertEqual(self.container_match_rc("sha256:" + H_B), "RC=0")

    def test_container_image_matches_rejects_unrelated(self):
        self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_B]),
        })
        self.assertEqual(self.container_match_rc("sha256:" + H_C), "RC=1")

    # --- install_callback：候选集下的跳过 / 导入复核 ----------------------

    def test_install_callback_classic_skips_load(self):
        # 经典 docker save：Config 为 <64hex>.json
        self.build_package({"manifest.json": manifest_json(H_A + ".json")}, gz=True)
        log = os.path.join(self.base, "docker.log")
        res = self.run_install("sha256:" + H_A, log)
        self.assertEqual(res.returncode, 0, res.stderr)
        cmds = self.read_commands(log)
        self.assertTrue(any("inspect" in c for c in cmds), cmds)
        self.assertFalse(any("rmi" in c for c in cmds), cmds)
        self.assertFalse(any("load" in c for c in cmds), cmds)

    def test_install_callback_oci_index_skips_load(self):
        # 候选为 [Config, index digest]；真实 .Id 只命中后者（旧单行逻辑会误判）
        self.build_package({
            "manifest.json": manifest_json("blobs/sha256/" + H_A),
            "index.json": index_json(["sha256:" + H_B]),
        }, gz=True)
        log = os.path.join(self.base, "docker.log")
        res = self.run_install("sha256:" + H_B, log)
        self.assertEqual(res.returncode, 0, res.stderr)
        cmds = self.read_commands(log)
        self.assertFalse(any("rmi" in c for c in cmds), cmds)
        self.assertFalse(any("load" in c for c in cmds), cmds)

    def test_install_callback_reloads_and_verifies(self):
        self.build_package({"manifest.json": manifest_json(H_A)}, gz=True)
        log = os.path.join(self.base, "docker.log")
        res = self.run_install("sha256:" + H_C, log, id_after="sha256:" + H_A)
        self.assertEqual(res.returncode, 0, res.stderr)
        cmds = self.read_commands(log)
        self.assertTrue(any("rmi" in c for c in cmds), cmds)
        self.assertTrue(any("load" in c for c in cmds), cmds)
        text = self.install_log_text()
        self.assertIn("Verified: loaded image id matches expected.", text)
        self.assertNotIn("WARN:", text)

    def test_install_callback_warns_on_post_load_mismatch(self):
        self.build_package({"manifest.json": manifest_json(H_A)}, gz=True)
        log = os.path.join(self.base, "docker.log")
        res = self.run_install("sha256:" + H_C, log, id_after="sha256:" + H_D)
        self.assertEqual(res.returncode, 0, res.stderr)
        text = self.install_log_text()
        self.assertIn("WARN: loaded image id", text)
        self.assertIn("does not match expected", text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线镜像指纹兼容性验证（隔离运行）")
    parser.add_argument("--source", default=default_source(),
                        help="fnos-app/cmd/main 路径")
    args = parser.parse_args(argv)

    source = os.path.abspath(args.source)
    if not os.path.isfile(source):
        print("NOT RUN: 找不到 --source 指定的 cmd/main：%s" % source)
        return 2
    install_callback = os.path.join(os.path.dirname(source), "install_callback")
    if not os.path.isfile(install_callback):
        print("NOT RUN: 找不到同目录的 install_callback：%s" % install_callback)
        return 2
    try:
        with open(source, "r", encoding="utf-8") as fh:
            text = fh.read()
        ImageFingerprintTests.PREFIX = extract_prefix(text)
    except (OSError, AssertionError) as exc:
        print("NOT RUN: 无法从源码抽取前缀：%s" % exc)
        return 2
    ImageFingerprintTests.MAIN = source
    ImageFingerprintTests.INSTALL_CALLBACK = install_callback

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ImageFingerprintTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
