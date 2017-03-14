package collectd

// #cgo CFLAGS: -I/usr/include/collectd -I/usr/include -I/usr/local/include/collectd -I/usr/local/include -DSIGNALFX_EIM=1
// #cgo LDFLAGS: /usr/local/lib/collectd/libcollectd.so
// #include <stdint.h>
// #include <stdlib.h>
// #include <string.h>
// #include "collectd.h"
// #include "configfile.h"
// #include "plugin.h"
import "C"
import (
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"path/filepath"

	"io/ioutil"

	"encoding/json"

	"github.com/signalfx/neo-agent/plugins"
	"github.com/signalfx/neo-agent/plugins/monitors/collectd/config"
	"github.com/signalfx/neo-agent/services"
	"github.com/spf13/viper"
)

const (
	pluginType = "monitors/collectd"

	// Running collectd
	Running = "running"
	// Stopped collectd
	Stopped = "stopped"
	// Reloading collectd plugins
	Reloading = "reloading"
)

// Collectd Monitor
type Collectd struct {
	plugins.Plugin
	state         string
	services      services.Instances
	templatesDirs []string
	confFile      string
	pluginsDir    string
	reloadChan    chan int
	stopChan      chan int
	templatesMap  map[string][]string
	configMutex   sync.Mutex
	configDirty   bool
}

func init() {
	plugins.Register(pluginType, NewCollectd)
}

// NewCollectd constructor
func NewCollectd(name string, config *viper.Viper) (plugins.IPlugin, error) {
	plugin, err := plugins.NewPlugin(name, pluginType, config)
	if err != nil {
		return nil, err
	}
	c := &Collectd{
		Plugin:     plugin,
		state:      Stopped,
		reloadChan: make(chan int),
		stopChan:   make(chan int)}
	if err := c.load(plugin.Config); err != nil {
		return nil, err
	}

	return c, nil
}

// load collectd plugin config from the provided config
func (collectd *Collectd) load(config *viper.Viper) error {
	var err error

	templatesDirs := config.GetStringSlice("templatesdirs")
	if len(templatesDirs) == 0 {
		return errors.New("config missing templatesDirs entry")
	}

	// Convert to absolute paths since our cwd can get changed.
	for i := range templatesDirs {
		absTemplateDir, err := filepath.Abs(templatesDirs[i])
		if err != nil {
			return err
		}
		templatesDirs[i] = absTemplateDir
	}

	confFile := config.GetString("conffile")
	if confFile == "" {
		return errors.New("config missing confFile entry")
	}

	confFile, err = filepath.Abs(confFile)
	if err != nil {
		return err
	}

	pluginsDir := config.GetString("pluginsDir")
	if err != nil {
		return err
	}

	templatesMapPath := config.GetString("templatesMap")
	if templatesMapPath == "" {
		return errors.New("config missing templatesMap entry")
	}

	templatesMap := map[string][]string{}
	if err := loadTemplatesMap(templatesMapPath, templatesMap); err != nil {
		return err
	}

	// Set values once everything passes muster.
	collectd.templatesDirs = templatesDirs
	collectd.confFile = confFile
	collectd.pluginsDir = pluginsDir
	collectd.templatesMap = templatesMap

	return nil
}

// loadTemplatesMap loads template mapping file from path into templatesMap
func loadTemplatesMap(path string, templatesMap map[string][]string) error {
	path, err := filepath.Abs(path)
	if err != nil {
		return err
	}

	data, err := ioutil.ReadFile(path)
	if err != nil {
		return err
	}

	if err = json.Unmarshal(data, &templatesMap); err != nil {
		return err
	}

	return nil
}

// Monitor services from collectd monitor
func (collectd *Collectd) Write(services services.Instances) error {
	collectd.configMutex.Lock()
	defer collectd.configMutex.Unlock()

	changed := false

	if collectd.configDirty {
		changed = true
		collectd.configDirty = false
	}

	if changed {
		log.Print("reloading config due to dirty flag")
	} else if len(collectd.services) != len(services) {
		changed = true
	} else {
		for i := range services {
			if services[i].ID != collectd.services[i].ID {
				changed = true
				break
			}
		}
	}

	if changed {
		servicePlugins, err := collectd.createPluginsFromServices(services)
		if err != nil {
			return err
		}

		plugins, err := collectd.getStaticPlugins()
		if err != nil {
			return err
		}

		collectd.services = services

		plugins = append(plugins, servicePlugins...)
		if err := collectd.writePlugins(plugins); err != nil {
			return err
		}

		collectd.reload()
	}

	return nil
}

// reload reloads collectd configuration
func (collectd *Collectd) reload() {
	if collectd.state == Running {
		collectd.setState(Reloading)
		collectd.reloadChan <- 1
		for collectd.Status() == Reloading {
			time.Sleep(time.Duration(1) * time.Second)
		}
	}
}

// writePlugins takes a list of plugin instances and generates a collectd.conf
// formatted configuration.
func (collectd *Collectd) writePlugins(plugins []*config.Plugin) error {
	collectdConfig := config.NewCollectdConfig()
	// If this is empty then collectd determines hostname.
	collectdConfig.Hostname = viper.GetString("hostname")

	config, err := config.RenderCollectdConf(collectd.pluginsDir, collectd.templatesDirs, &config.AppConfig{
		AgentConfig: collectdConfig,
		Plugins:     plugins,
	})

	if err != nil {
		return err
	}

	if err := ioutil.WriteFile(collectd.confFile, []byte(config), 0644); err != nil {
		return fmt.Errorf("failed to write collectd config: %s", err)
	}

	return nil
}

// getStaticPlugins returns a list of plugins specified in the agent config
func (collectd *Collectd) getStaticPlugins() ([]*config.Plugin, error) {
	static := struct {
		// This could possibly be cleaned up to use inline annotation but
		// haven't figured out how to make it work.
		StaticPlugins map[string]map[string]interface{}
	}{}

	if err := collectd.Config.Unmarshal(&static); err != nil {
		return nil, err
	}

	var plugins []*config.Plugin

	for pluginName, plugin := range static.StaticPlugins {
		yamlPluginType, ok := plugin["plugin"]
		if !ok {
			return nil, fmt.Errorf("static plugin %s missing plugin type", pluginName)
		}

		pluginType, ok := yamlPluginType.(string)
		if !ok {
			return nil, fmt.Errorf("static plugin %s is not a string", pluginName)
		}

		pluginInstance, err := config.NewPlugin(services.ServiceType(pluginType), pluginName)
		if err != nil {
			return nil, err
		}

		if err := config.LoadPluginConfig(map[string]interface{}{"config": plugin},
			pluginType, pluginInstance); err != nil {
			return nil, err
		}

		plugins = append(plugins, pluginInstance)
	}

	return plugins, nil
}

func (collectd *Collectd) createPluginsFromServices(sis services.Instances) ([]*config.Plugin, error) {
	log.Printf("Configuring collectd plugins for %+v", sis)
	var plugins []*config.Plugin

	for _, service := range sis {
		// TODO: Name is not unique, not sure what to use here.
		plugin, err := config.NewPlugin(service.Service.Type, service.Service.Name)
		if err != nil {
			log.Printf("unsupported service %s for collectd", service.Service.Type)
			continue
		}

		for key, val := range service.Orchestration.Dims {
			dim := key + "=" + val
			if len(plugin.Dims) > 0 {
				plugin.Dims = plugin.Dims + ","
			}
			plugin.Dims = plugin.Dims + dim
		}

		plugin.Host = service.Port.IP
		if service.Orchestration.PortPref == services.PRIVATE {
			plugin.Port = service.Port.PrivatePort
		} else {
			plugin.Port = service.Port.PublicPort
		}

		if plugin.Port == 0 {
			continue
		}

		log.Printf("reconfiguring collectd service: %s (%s)", service.Service.Name, service.Service.Type)

		if templates, ok := collectd.templatesMap[service.Service.Name]; ok {
			log.Printf("Replacing templates %s with %s for %s", plugin.Templates, templates, service.Service.Name)
			plugin.Templates = templates
		}

		plugins = append(plugins, plugin)
	}

	fmt.Printf("Configured plugins: %+v\n", plugins)

	return plugins, nil
}

// Start collectd monitoring
func (collectd *Collectd) Start() (err error) {
	println("starting collectd")
	if collectd.state == Running {
		return errors.New("already running")
	}

	collectd.services = nil

	log.Println("configuring static collectd plugins before first start")
	plugins, err := collectd.getStaticPlugins()
	if err != nil {
		return err
	}

	if err := collectd.writePlugins(plugins); err != nil {
		return err
	}

	go collectd.run()

	return nil
}

// Stop collectd monitoring
func (collectd *Collectd) Stop() {
	if collectd.state != Stopped {
		collectd.stopChan <- 0
	}
}

// Reload collectd config
func (collectd *Collectd) Reload(config *viper.Viper) error {
	collectd.configMutex.Lock()
	defer collectd.configMutex.Unlock()

	if err := collectd.load(config); err != nil {
		return err
	}

	collectd.Config = config
	collectd.configDirty = true
	return nil
}

// Status for collectd monitoring
func (collectd *Collectd) Status() string {
	return collectd.state
}

// Status for collectd monitoring
func (collectd *Collectd) setState(state string) {
	collectd.state = state
}

func (collectd *Collectd) run() {
	collectd.setState(Running)
	defer collectd.setState(Stopped)

	// TODO - global collectd interval should be configurable
	interval := time.Duration(10 * time.Second)
	cConfFile := C.CString(collectd.confFile)
	defer C.free(cConfFile)

	C.plugin_init_ctx()

	C.cf_read(cConfFile)

	C.init_collectd()
	C.interval_g = C.cf_get_default_interval()

	C.plugin_init_all()

	for {
		t := time.Now()

		C.plugin_read_all()

		remainingTime := interval - time.Since(t)
		if remainingTime > 0 {
			time.Sleep(remainingTime)
		}

		select {
		case <-collectd.stopChan:
			log.Println("stop collectd requested")
			C.plugin_shutdown_all()
			return
		case <-collectd.reloadChan:
			log.Println("reload collectd plugins requested")
			collectd.setState(Reloading)

			C.plugin_shutdown_for_reload()
			C.plugin_init_ctx()
			C.cf_read(cConfFile)
			C.plugin_init_for_reload()

			collectd.setState(Running)
		}
	}
}
