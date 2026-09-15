from scripts import update_dependencies


def test_checksum_for_archive_reads_named_checksum():
    checksum = "a" * 64
    assert (
        update_dependencies._checksum_for_archive(
            f"{checksum}  deno-x86_64-unknown-linux-gnu.zip\n",
            "deno-x86_64-unknown-linux-gnu.zip",
        )
        == checksum
    )


def test_checksum_for_archive_rejects_checksum_for_another_archive():
    checksum = "b" * 64
    try:
        update_dependencies._checksum_for_archive(
            f"{checksum}  deno-aarch64-unknown-linux-gnu.zip\n",
            "deno-x86_64-unknown-linux-gnu.zip",
        )
    except update_dependencies.UpdateError:
        pass
    else:
        raise AssertionError("checksum for a different archive should be rejected")
