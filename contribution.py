# coding: utf-8

import argparse
import asyncio
import getpass
import json
import re
import time
import warnings
import subprocess
import requests
import itertools

_BASE_CONTENT = '''# 开源项目贡献统计

## Contributions'''

_RE_URL_PROCESS = re.compile(r'^https://api.github.com/repos/([^/]+)/([^/]+)')
_RE_URL_REPLACE = r'https://github.com/\1/\2'
_RE_PULL_URL_PROCESS = re.compile(r'^https://api.github.com/repos/([^/]+)/([^/]+)/([^/]+)/([^/]+)')
_RE_PULL_URL_REPLACE = r'https://github.com/\1/\2/pull/\4'

_DEFAULT_TEMPLATE = '* [**{repo_name}**(★{star})]({repo_url}) - [{title}]({url})'
_ONLY_REPO_TEMPLATE = '* [**{repo_name}**(★{star})]({repo_url})'
_ONLY_PR_TEMPLATE = '  * [{title}]({url})'


def _step(desc, *args, **kwargs):
    print(desc.format(*args, **kwargs), '...', end='', flush=True)


def _ok():
    print('OK')


def _api_url_to_normal(url):
    return _RE_URL_PROCESS.sub(_RE_URL_REPLACE, url)


class _User(object):
    def __init__(self, url, name):
        self.url = url
        self.name = name


class _Repo(object):
    def __init__(self, url, name='', author=None, star=0):
        self.url = url
        self.name = name
        self.author = author
        self.star = star


class _PullRequest(object):
    def __init__(self, url, title, repo):
        self.url = url
        self.title = title
        self.repo = repo

    def format(self, template, custom_data=None):
        context = {
            'url': _RE_PULL_URL_PROCESS.sub(_RE_PULL_URL_REPLACE, self.url),
            'title': self.title,
            'repo_name': self.repo.name,
            'repo_url': _api_url_to_normal(self.repo.url),
            'star': self.repo.star,
            'repo_author_name': self.repo.author.name,
            'repo_author_url': _api_url_to_normal(self.repo.author.url),
        }

        if custom_data is not None:
            context.update({'c': custom_data(self)})

        res = template.format(**context)
        return res


class ContributionsCrawler(object):
    __GITHUB_API_ROOT = 'https://api.github.com'
    __GITHUB_API_TEST_LOGIN = __GITHUB_API_ROOT + '/user'
    __GITHUB_API_SEARCH = __GITHUB_API_ROOT + '/search/issues'

    def __init__(self, token=None, username=None, password=None, sort='created',
                 asc=False, async_mode=False, async_pool=None, exclude=None):
        """
        The crawler to get all your contributions.

        :param str token: The GitHub OAuth Token
        :param str username: The GitHub username to
        :param str password: The GitHub password for [username]

        :param str sort: Pick one from {created, updated, comment}
        :params bool asc: Use asc order, False will be desc
        :param bool async_mode: use async mode (need Python 3.5+)
        :param async_pool: if not given, will use default pool of aiohttp
        :param str exclude: a regex string to exclude prs
            which's repo title match it
        """
        if sort not in {'comment', 'created', 'updated'}:
            raise ValueError('Invalid sort param')

        if token is None:
            self.__username = username
            self.__password = password
            self.__headers = {'User-Agent': self.__username}
        else:
            self.__headers = {'Authorization': 'token {}'.format(token)}
            resp = requests.get(self.__GITHUB_API_TEST_LOGIN, headers=self.__headers)
            user_info = self.__get_json_or_error(resp, prefix_message='Login failed: ')
            self.__username = user_info['login']

        query = [('author', str(self.__username)), ('type', 'pr'), ('is', 'merged')]
        self.__params = {
            'q': self.__build_query_string(query),
            'sort': sort,
            'order': 'asc' if asc else 'desc',
            'per_page': 100,
        }

        self.__async_mode = bool(async_mode)
        self.__async_pool = async_pool
        if exclude is not None:
            self.__exclude = re.compile(exclude)
        else:
            self.__exclude = None

    def __is_exclude(self, repo_url):
        repo_name = _RE_URL_PROCESS.sub(r'\1/\2', repo_url)
        if self.__exclude:
            return self.__exclude.search(repo_name) is not None

    @staticmethod
    def __pr_obj(pr_data, pr_url):
        return _PullRequest(pr_url, pr_data['title'], None)

    @staticmethod
    def __repo_add_user(pr_data, repo):
        user_data = pr_data['base']['user']
        user_url = user_data['url']
        user_name = user_data['login']
        repo.author = _User(user_url, user_name)

    @staticmethod
    def __build_repo_from_data(pr_data, repo_url):
        repo_data = pr_data['base']['repo']
        name = repo_data['full_name']
        star = repo_data['stargazers_count']
        repo = _Repo(repo_url, name, None, star)
        return repo

    @staticmethod
    def __build_query_string(query):
        return ' '.join([':'.join(item) for item in query])

    async def run(self):
        if self.__async_mode:
            print('-----ASYNC MODE-----')
            print('If error happened, please add --sync option and try again')
            return await self.__run_async()
        else:
            print("-----SYNC MODE-----")
            return self.__run_sync()

    def __build_session(self):
        from requests import Session
        session = Session()

        def new_get(*args, **kwargs):
            while True:
                res = session.old_get(*args, **kwargs)
                error = self.__get_json_or_error(res, raise_error=False)
                if isinstance(error, RuntimeError) and 'API limits' in str(error):
                    time.sleep(5)
                    continue
                return res

        session.auth = (self.__username, self.__password)
        session.headers = {'User-Agent': self.__username}

        session.old_get = session.get
        session.get = new_get

        return session

    @staticmethod
    def __get_json_or_error(
            resp, cls=RuntimeError,
            prefix_message='GitHub API Error: ', after_message='',
            raise_error=True,
    ):
        try:
            json_data = resp.json()
            if 200 <= resp.status_code < 300:
                return json_data
            else:
                msg = prefix_message + json_data['message'] + after_message
        except json.JSONDecodeError:
            msg = 'GitHub return a non-json response: ' + resp.text
        except KeyError:
            msg = 'GitHub error json has no message field: ' + resp.text

        if raise_error:
            raise cls(msg)
        else:
            return cls(msg)

    def __test_login(self, session):
        _step("Login to GitHub as [{}]", self.__username)
        resp = session.get(self.__GITHUB_API_TEST_LOGIN)
        self.__get_json_or_error(resp, prefix_message='Login failed: ')
        _ok()

    def __build_pr(self, session, pr_url, repo_url, repos):
        pr_data = self.__get_json_or_error(session.get(pr_url))
        pr = self.__pr_obj(pr_data, pr_url)
        _step('title is [{}]', pr.title)
        if repo_url not in repos:
            _ok()
            repo = self.__build_repo_from_data(pr_data, repo_url)
            _step('Getting new repo [{}] data', repo.name)
            self.__repo_add_user(pr_data, repo)
            repos[repo_url] = repo
        else:
            _step('of gotten repo [{}]', repos[repo_url].name)
        pr.repo = repos[repo_url]
        return pr

    def __run_sync(self):
        session = self.__build_session()
        self.__test_login(session)

        prs = []
        repos = {}
        params = self.__params.copy()
        params['page'] = 1
        excluded = 0

        # get all pr data
        while True:
            _step('Getting PR pages, current page {}', params['page'])

            resp = session.get(self.__GITHUB_API_SEARCH, params=params)
            data = self.__get_json_or_error(resp)
            total = data['total_count'] - excluded

            _step('total count: {}', total)
            _ok()

            for issue_data in data['items']:
                repo_url = issue_data['repository_url']
                pr_url = issue_data['pull_request']['url']

                _step('Getting {}/{} PR data', len(prs) + 1, total)

                if self.__is_exclude(pr_url):
                    excluded += 1
                    total -= 1
                    print('exclude')
                    continue

                prs.append(self.__build_pr(session, pr_url, repo_url, repos))
                _ok()

            if len(prs) == total:
                break

            params['page'] += 1

        return prs

    def __build_async_session(self):
        from aiohttp import ClientSession, BasicAuth

        if hasattr(self, '__password'):
            return ClientSession(auth=BasicAuth(self.__username, self.__password), headers=self.__headers)
        else:
            return ClientSession(headers=self.__headers)

    @staticmethod
    async def __get_json_or_error_async(
            resp, cls=RuntimeError,
            prefix_message='GitHub API Error: ', after_message='',
            raise_error=True,
    ):
        try:
            json_data = await resp.json()
            if 200 <= resp.status < 300:
                return json_data
            else:
                msg = prefix_message + json_data['message'] + after_message
        except json.JSONDecodeError:
            msg = 'GitHub return a non-json response: ' + resp.text
        except KeyError:
            msg = 'GitHub error json has no message field: ' + resp.text

        if raise_error:
            raise cls(msg)
        else:
            return cls(msg)

    async def __test_login_async(self, session):
        _step("Login to GitHub as [{}]", self.__username)
        async with session.get(self.__GITHUB_API_TEST_LOGIN) as resp:
            try:
                await self.__get_json_or_error_async(
                    resp, prefix_message='Login failed: '
                )
            except Exception as e:
                session.close()
                raise e

    async def __pr_worker(self, session, pr_url, repo_url):
        async with session.get(pr_url) as resp:
            pr_data = await self.__get_json_or_error_async(resp)
            pr = self.__pr_obj(pr_data, pr_url)

            repo = self.__build_repo_from_data(pr_data, repo_url)
            self.__repo_add_user(pr_data, repo)

            pr.repo = repo
            return pr

    async def __run_async(self):
        session = self.__build_async_session()
        await self.__test_login_async(session)

        prs = []
        params = self.__params.copy()
        params['page'] = 1
        exclude = 0

        # create pr tasks
        while True:
            resp = await session.get(self.__GITHUB_API_SEARCH, params=params)
            data = await self.__get_json_or_error_async(resp)
            total = data['total_count'] - exclude

            for issue_data in data['items']:
                pr_url = issue_data['pull_request']['url']
                repo_url = issue_data['repository_url']

                _step('\rAdd task {i}/{total}', i=len(prs) + 1, total=total)

                if self.__is_exclude(repo_url):
                    exclude += 1
                    total -= 1
                    continue

                prs.append(asyncio.ensure_future(
                    self.__pr_worker(session, pr_url, repo_url)
                ))

            if len(prs) == total:
                _ok()
                break

            params['page'] += 1

        for i, future in enumerate(prs):
            _step('\rWaiting for task {i}/{total}', i=i + 1, total=total)
            prs[i] = await future

        _ok()
        session.close()

        return prs

    def write(self, prs, template=None, filename='README.md',
              custom_data=None):

        template = template or _DEFAULT_TEMPLATE

        _step('Building PR data', filename=filename)

        prs = list(filter(lambda x: not x.repo.name.startswith('{}/'.format(self.__username)), prs))

        content_list = ["({} merged)\n\n".format(len(prs))]
        for repo, pr in itertools.groupby(sorted(prs, key=lambda x: x.repo.name), key=lambda x: x.repo.name):
            pr = list(pr)
            if len(pr) == 1:
                content_list.extend([x.format(template, custom_data) for x in pr])
            else:
                content_list.append(pr[0].format(_ONLY_REPO_TEMPLATE, custom_data))
                content_list.extend([x.format(_ONLY_PR_TEMPLATE, custom_data) for x in pr])

        content_list.append('')
        _ok()

        _step('Writing data to {}', filename)
        with open(filename, 'w') as f:
            f.writelines(_BASE_CONTENT + '\n'.join(content_list))
        _ok()

    def push(self):
        _step("Push to Github")
        subprocess.run(["git", "add", "README.md"])
        subprocess.run(["git", "commit", "-m", "update README.md"])
        subprocess.run(["git", "push"])

    async def run_and_write(self, template=None, filename='README.md'):
        self.write(await self.run(), template, filename)
        # self.push()


async def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-t', '--token', type=str, help='OAuth Token'
    )
    parser.add_argument(
        '-s', '--sync', action='store_true', help='Use sync mode'
    )
    parser.add_argument(
        '-e', '--exclude', type=str,
        help='Exclude PRs which\'s repo full name match the regex'
    )
    parser.add_argument(
        '-o', '--output', type=str, default='README.md',
        help='Output filename, default will be README.md',
    )

    args = parser.parse_args()

    if args.token is None:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', getpass.GetPassWarning)
            login_user = input('Username for Github:')
            password = getpass.getpass('Password for Github:')
        c = ContributionsCrawler(username=login_user, password=password, async_mode=not args.sync, exclude=args.exclude)
    else:
        c = ContributionsCrawler(token=args.token, async_mode=not args.sync, exclude=args.exclude)

    await c.run_and_write(filename=args.output)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
    loop.close()
