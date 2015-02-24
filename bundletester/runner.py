import datetime
import logging
import os
import subprocess
import traceback

from bundletester import builder, models
from bundletester.spec import Suite

log = logging.getLogger('runner')


def relative_to(filenames, basefile):
    """Normalize files relative to basefile turning partial names into files in
    the same dir as basefile
    """
    results = []
    if isinstance(basefile, list):
        basefile = basefile[0]
    if basefile is None:
        return results
    dirname = os.path.dirname(basefile)
    for f in filenames:
        if isinstance(f, list):
            f = f[0]
        path = os.path.abspath(os.path.join(dirname, f))
        if os.path.exists(path):
            results.append(path)
    return results


class DeployError(Exception):
    pass


class Runner(object):
    def __init__(self, suite, options=None):
        self.suite = suite
        self._builder = None
        self.options = options

    @property
    def builder(self):
        if not self._builder:
            self._builder = builder.Builder(self.suite.config, self.options)
        return self._builder

    def _run(self, executable):
        log.debug("call %s" % executable)
        if self.options.dryrun:
            return 0, ""

        p = subprocess.Popen(
            executable,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.options.testdir,
        )

        # Print all output as it comes in to debug
        output = []
        lines = iter(p.stdout.readline, "")
        for line in lines:
            output.append(line)
            log.debug(str(line.rstrip()))

        p.communicate()
        retcode = p.returncode
        log.debug("Exit Code: %s" % retcode)
        return retcode, ''.join(output)

    def run(self, spec, phase=None):
        """Run a phase of spec.

        If no phase is provided spec's main test will execute.
        """
        result = {
            'test': spec.name,
            'returncode': 0
        }

        if phase == "setup":
            canidates = relative_to(spec.setup, spec.suite.testdir)
        elif phase == "teardown":
            canidates = relative_to(reversed(spec.teardown),
                                    spec.suite.testdir)
        else:
            canidates = [spec.executable]

        if not canidates:
            return result
        start = datetime.datetime.utcnow()
        for canidate in canidates:
            ec, output = self._run(canidate)
            result['returncode'] = ec
            result['output'] = output
            result['executable'] = spec.executable
            if ec != 0:
                if isinstance(canidate, list):
                    canidate = " ".join(canidate)
                result['exit'] = canidate
                break

        if not phase:
            end = datetime.datetime.utcnow()
            duration = end - start
            result['duration'] = duration.total_seconds()
            if result['duration'] < 0.1:
                result['duration'] = 0.0
        return result

    def build(self):
        # if we are already in a venv we will assume we
        # can use that
        if self.suite.config.virtualenv and not os.environ.get("VIRTUAL_ENV"):
            vpath = os.path.join(self.options.testdir, '.venv')
            self.builder.build_virtualenv(vpath)
            apath = os.path.join(vpath, 'bin/activate_this.py')
            execfile(apath, dict(__file__=apath))

        self.builder.add_sources(self.suite.config.sources)
        self.builder.install_packages()

    def _handle_result(self, result):
        stop = False
        if self.options and self.options.failfast and \
                result.get('returncode', 1) != 0:
            stop = True
        return result, stop

    def __call__(self):
        self.build()
        bootstrapped = self.builder.bootstrap()
        if isinstance(self.suite.model, models.Bundle) and\
           self.options.bundle_deploy:
            self._deploy(self.suite.model['bundle'])
        for element in self.suite:
            if isinstance(element, Suite):
                for result in self._run_suite(element):
                    result, stop = self._handle_result(result)
                    yield result
                    if stop:
                        raise StopIteration
            else:
                result, stop = self._handle_result(self._run_test(element))
                yield result
                if stop:
                    raise StopIteration

        if bootstrapped:
            self.builder.destroy()

    def _deploy(self, bundle):
        deployed = self.builder.deploy(bundle)
        if not deployed or deployed and deployed.get('returncode') != 0:
            exc = DeployError()
            exc.result = result = {}
            result['test'] = 'juju-deployer'
            result['suite'] = 'bundletester'
            result['exit'] = 'juju-deployer'
            exc.result.update(deployed)
            raise exc

    def _run_suite(self, suite):
        for spec in suite:
            yield self._run_test(spec)

    def _run_test(self, spec):
        result = {}
        cwd = os.getcwd()
        try:
            if spec.reset:
                self.builder.reset()
            basedir = spec.get('dirname')
            if basedir:
                result['dirname'] = basedir
                os.chdir(basedir)
            result.update(self.run(spec, 'setup'))
            if result.get('returncode', 0) == 0:
                result.update(self.run(spec))
        except DeployError, e:
            result.update(e.result)
        except KeyboardInterrupt:
            result['returncode'] = 1
        except subprocess.CalledProcessError, e:
            result['returncode'] = e.returncode
            result['output'] = e.output
            result['executable'] = e.cmd
        except Exception, e:
            log.exception(e)
            result['returncode'] = 1
            result['output'] = '{}\n{}'.format(
                result.get('output', ''), traceback.format_exc())
        finally:
            os.chdir(cwd)
            td = self.run(spec, 'teardown')
            if td.get('returncode') != 0:
                log.error('Failed to teardown test %s' % spec)
                # Only in the event of td failure do we update result
                # otherwise a successful teardown could overwrite
                # the failure code of a main phase test
                result.update(td)
            suite = spec.get('suite')
            result['suite'] = suite and suite.name or None
            return result
