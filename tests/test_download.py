from data_prep.download import BD_FILES, webdav_url


def test_webdav_url_built_from_share_url():
    url, user = webdav_url("https://cloud.example/s/TOKEN", "BD/X.zip")
    assert url == "https://cloud.example/public.php/webdav/2025/1/BD/X.zip"
    assert user == "TOKEN"


def test_webdav_url_strips_trailing_slash():
    url, user = webdav_url("https://cloud.example/s/TOKEN/", "README.txt")
    assert url == "https://cloud.example/public.php/webdav/2025/1/README.txt"
    assert user == "TOKEN"


def test_bd_files_are_the_two_release_zips():
    assert len(BD_FILES) == 2
    assert all(name.startswith("WN_L2_V8_") and name.endswith("_UTC.zip") for name in BD_FILES)
