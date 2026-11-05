# Contributing to precis-mcp

Thanks for your interest in contributing.

This project adheres to a [Code of Conduct](./CODE_OF_CONDUCT.md) — by
participating, you are expected to uphold it.

## Licensing of contributions

`precis-mcp` is licensed under the **Elastic License 2.0 (ELv2)** — see
[`LICENSE`](./LICENSE). By submitting a contribution, you agree that it is
licensed under ELv2 on the same terms as the rest of the project.
Third-party portions retain their existing terms; the Excel add-in's Microsoft
template attribution is in [`excel-addin/THIRD_PARTY_NOTICES.txt`](excel-addin/THIRD_PARTY_NOTICES.txt).

## Sign your work — the Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) rather than a Contributor License Agreement. The DCO is a lightweight
statement that you wrote the patch — or otherwise have the right to submit it
under the project's license. There is nothing to sign and no account to create:
you certify it per commit with a sign-off line.

Add the sign-off automatically with the `-s` flag:

```
git commit -s -m "your message"
```

This appends a line to your commit message:

```
Signed-off-by: Your Name <your.email@example.com>
```

Use your real name (no pseudonyms or anonymous contributions) and a reachable
email. By signing off you certify the DCO (full text at the link above):

> By making a contribution to this project, I certify that the contribution was
> created in whole or in part by me and I have the right to submit it under the
> open source license indicated in the file; or it is based on previous work
> that, to the best of my knowledge, is covered under an appropriate license and
> I have the right under that license to submit that work; or the contribution
> was provided directly to me by some other person who certified the above and I
> have not modified it. I understand and agree that this project and the
> contribution are public and that a record of the contribution (including all
> personal information I submit with it) is maintained indefinitely.

Pull requests whose commits are not signed off will be asked to amend them
(`git commit --amend -s`, or `git rebase --signoff` for a series).

## Tests and reusable helpers

`tests/open_tests.txt` lists the public test files. `tests/open_support.txt`
lists their reusable fixtures, fakes and factories; an optional second column
maps a source path to its published path. Paths are relative to `tests/`.
Declare new helpers in this manifest so import-boundary checks include them.
Shared pytest fixtures live in `tests/support/open_pytest.py`; the `db` fixture
uses the public `FakePlatformDB` in `tests/fakes/fake_platform_db.py`.

Run `make test-pre-push` for unit and component checks. The same tests and
helpers must work from the published repository without additional application
packages.

The core `.[dev]` installation skips checks that require optional Databricks or
telemetry dependencies. To include those checks, install
`pip install -c constraints-extras.txt ".[dev,databricks-primary,telemetry]"`
before running the suite.

## Dependency locks

Use Python 3.12 with `pip-tools` installed. `make lock` regenerates the hashed
core dependency lock, `requirements.lock`. After updating that lock, run
`make lock-extras` to regenerate `constraints-extras.txt` for the optional
warehouse drivers against the same core versions. Review and commit both
outputs together. Optional driver constraints are version-pinned; the core
lock is hashed. These commands do not install the resolved dependencies.
