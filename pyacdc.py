# pyAC-DC.py
# Programa para a medição de diferença RF-AC em conversores térmicos (TCs)
# O programa aceita TCs com saída em tensão, frequência e resistência.
# modificado em outubro de 2023 para calibrar TVCs Fluke A55 acima de 1 MHz
# usando gerador Keysight 33600A como fonte (AC e RF)
# usando dois DVMs (std e dut)
#-------------------------------------------------------------------------------
# Autor:       Gean Marcos Geronymo
#
# Versão inicial:      10-Jun-2016
# Última modificação:  06-Nov-2023
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Nomenclatura de variáveis:
#
# Adotou-se a convenção de utilizar X para as variáveis referentes ao padrão
# e Y para as variáveis referentes ao objeto.
#
# Por exemplo:
#
# Xac - leitura do padrão (std) quando submetido a Vac
# Xdc - leitura do padrão (std) quando submetido a Vdc
# Yac - leitura do objeto (dut) quando submetido a Vac
# Ydc - leitura do objeto (dut) quando submetido a Vdc
#
# O instrumento que lê a saída do padrão é identificado
# como 'std' e o instrumento que lê a saída do objeto
# como 'dut'.
#
# Comandos da chave
# os comandos sao enviados em formato ASCII puro
# utilizar os comandos
# sw.write_raw(chr(2)) (reset)
# sw.write_raw(chr(4)) (ac)
# sw.write_raw(chr(6)) (dc)
# chr(argumento) converte o valor binario em ascii
#-------------------------------------------------------------------------------
# versão do programa
versao = '0.1';
#-------------------------------------------------------------------------------
# Carregar módulos
import pyvisa as visa
import datetime
import configparser
import time
import numpy
import csv
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Constantes e variáveis globais
# comandos da chave (em ASCII puro)
reset = chr(2)
ac = chr(4)
dc = chr(6)
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Configurações
#-------------------------------------------------------------------------------
# o arquivo settings.ini reune as configurações que podem ser alteradas
config = configparser.ConfigParser() # iniciar o objeto config
config.read('config.ini') # ler o arquivo de configuracao
wait_time = int(config['Measurement Config']['wait_time']); # tempo de espera
heating_time = int(config['Measurement Config']['aquecimento']); # tempo de aquecimento
rm = visa.ResourceManager('@py')
repeticoes = int(config['Measurement Config']['repeticoes']); # quantidade de repetições
vac_nominal = float(config['Measurement Config']['voltage']); # Tensão nominal RF (>= 1 MHz)
vdc_nominal = float(config['Measurement Config']['voltage']); # Tensão nominal AC (100 kHz)
freq_array = config['Measurement Config']['frequency'].split(',') # Array com as frequências
r_dut = float(config['Measurement Config']['r_dut'])
r_std = float(config['Measurement Config']['r_std'])
delta_max_ppm = float(config['Measurement Config'].get('delta_max_ppm', '150'))
measurement_cycle = config['Measurement Config'].get('measurement_cycle', 'RF-AC-RF-AC-RF').strip().upper()
use_bme280 = config.getboolean('Misc', 'use_bme280', fallback=False)
std_model = config['Instruments'].get('std', '2182A').strip().upper()
dut_model = config['Instruments'].get('dut', '2182A').strip().upper()
if config.has_section('Sources'):
    source_mode = config['Sources'].get('mode', 'shared').strip().lower()
    ac_source_model = config['Sources'].get('ac_source', '33600A').strip().upper()
    rf_source_model = config['Sources'].get('rf_source', '33600A').strip().upper()
else:
    source_mode = 'shared'
    ac_source_model = '33600A'
    rf_source_model = '33600A'
load = str(int(1 / ( (1/r_dut) + (1/r_std) )))

if measurement_cycle == 'RF-AC-RF-AC-RF':
    cycle_sequence = ['RF', 'AC', 'RF', 'AC', 'RF']
elif measurement_cycle == 'AC-RF-AC':
    cycle_sequence = ['AC', 'RF', 'AC']
else:
    raise NameError('Ciclo de medicao invalido. Use RF-AC-RF-AC-RF ou AC-RF-AC em Measurement Config/measurement_cycle.')

rf_indices = [i for i, c in enumerate(cycle_sequence) if c == 'RF']
ac_indices = [i for i, c in enumerate(cycle_sequence) if c == 'AC']
cycle_csv_labels = ['RF' if c == 'RF' else 'AC 100 kHz' for c in cycle_sequence]

if use_bme280:
    import smbus2
    import bme280

console = Console()
ui = None


class MeasurementUI:
    def __init__(self):
        self.status = "Inicializando..."
        self.wait_message = "-"
        self.current_frequency = "-"
        self.current_vdc = "-"
        self.current_vac = "-"
        self.current_repeat = "-"
        self.total_repeats = repeticoes
        self.n_std = "-"
        self.n_dut = "-"
        self.programmed_frequencies_mhz = []
        self.programmed_vdc = vdc_nominal
        self.programmed_vac = vac_nominal
        self.cycle_rows = []
        self.results_rows = []
        self.summary_rows = []
        self.live = None

    def start(self):
        self.live = Live(self.render(), refresh_per_second=4, auto_refresh=False, console=console)
        self.live.start()

    def stop(self):
        if self.live is not None:
            self.live.stop()
            self.live = None

    def set_status(self, message):
        self.status = message
        self.refresh()

    def set_wait(self, message):
        self.wait_message = message
        self.refresh()

    def clear_wait(self):
        self.wait_message = "-"
        self.refresh()

    def set_frequency(self, frequency_mhz):
        self.current_frequency = "{:.0f} MHz".format(frequency_mhz)
        self.start_cycle_table()
        self.results_rows = []
        self.refresh()

    def start_cycle_table(self, first_std=None, first_dut=None):
        self.cycle_rows = []
        for label in cycle_csv_labels:
            self.cycle_rows.append({'cycle': label, 'std': None, 'dut': None})
        if first_std is not None and first_dut is not None and self.cycle_rows:
            self.cycle_rows[0]['std'] = first_std
            self.cycle_rows[0]['dut'] = first_dut
        self.refresh()

    def set_program(self, frequencies_mhz, vdc_programmed, vac_programmed):
        self.programmed_frequencies_mhz = frequencies_mhz[:]
        self.programmed_vdc = vdc_programmed
        self.programmed_vac = vac_programmed
        self.refresh()

    def set_setpoints(self, current_vdc, current_vac):
        self.current_vdc = "{:.4f} V".format(current_vdc)
        self.current_vac = "{:.4f} V".format(current_vac)
        self.refresh()

    def set_repetition(self, current_repeat, total_repeats):
        self.current_repeat = str(current_repeat)
        self.total_repeats = total_repeats
        self.refresh()

    def set_n_values(self, n_std, n_dut):
        self.n_std = "{:.3f}".format(n_std)
        self.n_dut = "{:.3f}".format(n_dut)
        self.refresh()

    def add_cycle_reading(self, cycle_index, std_value, dut_value):
        if cycle_index < len(self.cycle_rows):
            self.cycle_rows[cycle_index]['std'] = std_value
            self.cycle_rows[cycle_index]['dut'] = dut_value
        self.refresh()

    def add_result(self, dif_value, delta_value, discarded):
        self.results_rows.append({
            'dif': dif_value,
            'delta': delta_value,
            'discarded': discarded,
        })
        self.refresh()

    def add_frequency_summary(self, frequency_mhz, mean_value, std_value):
        self.summary_rows.append({
            'frequency_mhz': frequency_mhz,
            'mean': mean_value,
            'std': std_value,
        })
        self.refresh()

    def refresh(self):
        if self.live is not None:
            self.live.update(self.render(), refresh=True)

    def render(self):
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=16),
            Layout(name="mid", size=18),
            Layout(name="bottom", size=10)
        )

        layout["mid"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=3)
        )

        status_text = "Frequencia atual: {}\nTensao AC atual: {}\nTensao RF atual: {}\nMensagem do sistema:\n{}".format(
            self.current_frequency,
            self.current_vdc,
            self.current_vac,
            self.status,
        )
        program_table = Table(show_header=True, header_style="bold")
        program_table.add_column("Freq. programadas [MHz]", justify="right")
        if self.programmed_frequencies_mhz:
            current = None
            if self.current_frequency != "-":
                current = float(self.current_frequency.split()[0])
            for f in self.programmed_frequencies_mhz:
                label = "{:.0f}".format(f)
                if current is not None and abs(f - current) < 1e-9:
                    label = "[bold cyan]> {} <[/bold cyan]".format(label)
                program_table.add_row(label)
        else:
            program_table.add_row("-")
        program_table.add_row(" ")
        program_table.add_row("Vdc nominal: {:.4f} V".format(self.programmed_vdc))
        program_table.add_row("Vac nominal: {:.4f} V".format(self.programmed_vac))

        top_layout = Layout()
        top_layout.split_row(Layout(name="status", ratio=2), Layout(name="program", ratio=1), Layout(name="params", ratio=1))
        top_layout["status"].update(Panel(status_text, title="Controle das Medicoes", border_style="cyan"))
        top_layout["program"].update(Panel(program_table, title="Programa da Medicao", border_style="yellow"))

        param_table = Table(show_header=False)
        param_table.add_column("k", style="bold")
        param_table.add_column("v", justify="right")
        param_table.add_row("Espera", "[green]{}[/green]".format(self.wait_message))
        param_table.add_row("Repeticoes", "[green]{}/{}[/green]".format(self.current_repeat, self.total_repeats))
        param_table.add_row("n Padrao", self.n_std)
        param_table.add_row("n Objeto", self.n_dut)
        top_layout["params"].update(Panel(param_table, title="Parametros", border_style="white"))
        layout["top"].update(top_layout)

        cycle_table = Table(show_header=True, header_style="bold")
        cycle_table.add_column("Ciclo", justify="left")
        cycle_table.add_column("STD [mV]", justify="right", style="green")
        cycle_table.add_column("DUT [mV]", justify="right", style="green")
        for row in self.cycle_rows:
            std_value = "-" if row['std'] is None else "{:,.6f}".format(row['std']).replace(',', 'X').replace('.', ',').replace('X', '.')
            dut_value = "-" if row['dut'] is None else "{:,.6f}".format(row['dut']).replace(',', 'X').replace('.', ',').replace('X', '.')
            cycle_table.add_row(
                row['cycle'],
                std_value,
                dut_value,
            )
        layout["left"].update(Panel(cycle_table, title="Leituras Instantaneas", border_style="green"))

        results_table = Table(show_header=True, header_style="bold")
        results_table.add_column("Dif. RF-AC [µV/V]", justify="right")
        results_table.add_column("Delta [µV/V]", justify="right")
        results_table.add_column("Status", justify="center")
        for row in self.results_rows:
            status = "[red]DESCARTADO[/red]" if row['discarded'] else "[green]ACEITO[/green]"
            results_table.add_row(
                "{:,.2f}".format(row['dif']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                "{:,.2f}".format(row['delta']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                status,
            )
        layout["right"].update(Panel(results_table, title="Resultados da Medicao", border_style="magenta"))

        summary_table = Table(show_header=True, header_style="bold")
        summary_table.add_column("Frequencia [MHz]", justify="right")
        summary_table.add_column("Media RF-AC [µV/V]", justify="right")
        summary_table.add_column("Desvio padrao [µV/V]", justify="right")
        if self.summary_rows:
            for row in self.summary_rows:
                summary_table.add_row(
                    "{:.0f}".format(row['frequency_mhz']),
                    "{:,.2f}".format(row['mean']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                    "{:,.2f}".format(row['std']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                )
        else:
            summary_table.add_row("-", "-", "-")
        layout["bottom"].update(Panel(summary_table, title="Resumo da Medicao", border_style="blue"))
        return layout

#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Definições das funções
#-------------------------------------------------------------------------------
# função espera(segundos)
# aceita como parâmetro o tempo de espera, em segundos
# hack para poder interromper o programa a qualquer momento
# no Windows XP, a função time.sleep não pode ser interrompida por uma
# interrupção de teclado. A função quebra a chamada dessa função em várias
# chamadas de 0,1 segundo.
def espera(segundos):
    remaining = int(segundos)
    while remaining > 0:
        if ui is not None:
            ui.set_wait("{} s".format(remaining))
        time.sleep(1)
        remaining -= 1
    if ui is not None:
        ui.clear_wait()
    return
#-------------------------------------------------------------------------------
# inicializar bme280
def bme280_init():
    global port;
    port = 1;
    global address;
    address = 0x76;
    global bus;
    bus = smbus2.SMBus(port);
    global calibration_params;
    calibration_params = bme280.load_calibration_params(bus, address)

    return

def bme280_read():
    return bme280.sample(bus, address, calibration_params)

def configure_voltmeter(meter, model):
    if model == '182A':
        meter.write("X")
        meter.write("R0I0B1X")
        meter.write("O1P2X")
        print("Keithley 182A...\n")
    elif model == '2182A':
        meter.write("SENS:CHAN 1")
        meter.write(":SENS:VOLT:CHAN1:RANG:AUTO ON")
        meter.write(":SENS:VOLT:NPLC 18")
        meter.write(":SENS:VOLT:DIG 8")
    print(meter.query("*IDN?"))
    print("OK!\n")

def read_voltmeter(meter, model):
    if model == '182A':
        return meter.query("X")
    if model == '2182A':
        return meter.query(":FETCH?")
    try:
        return meter.query("READ?")
    except Exception:
        return meter.query(":FETCH?")

def set_ac_voltage_and_frequency(voltage, frequency=100000):
    if source_mode == 'shared':
        if ac_source_model == '33600A':
            ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(voltage))
            ac_source.write("SOUR1:FREQ {:.0f}".format(frequency))
        else:
            raise NameError('Modo shared suporta apenas fonte 33600A.')
    else:
        if ac_source_model == '33600A':
            ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(voltage))
            ac_source.write("SOUR1:FREQ {:.0f}".format(frequency))
        elif ac_source_model == '5700A':
            ac_source.write("OUT {:.6f} V, {:.0f} HZ".format(voltage, frequency))
        else:
            raise NameError('Modelo de fonte AC não suportado: {}'.format(ac_source_model))

def set_rf_voltage_and_frequency(voltage, frequency):
    if source_mode == 'shared':
        if rf_source_model == '33600A':
            ac_source.write("SOUR2:VOLT {:.3f} VRMS".format(voltage))
            ac_source.write("SOUR2:FREQ {:.0f}".format(frequency))
        else:
            raise NameError('Modo shared suporta apenas fonte 33600A no canal RF.')
    else:
        if rf_source_model == '33600A':
            rf_source.write("SOUR1:VOLT {:.3f} VRMS".format(voltage))
            rf_source.write("SOUR1:FREQ {:.0f}".format(frequency))
        else:
            raise NameError('Modelo de fonte RF não suportado: {}'.format(rf_source_model))

def sources_output_on():
    if source_mode == 'shared':
        ac_source.write("OUTP1 ON")
        ac_source.write("OUTP2 ON")
    else:
        if ac_source_model == '33600A':
            ac_source.write("OUTP1 ON")
        elif ac_source_model == '5700A':
            ac_source.write("OPER")
        if rf_source_model == '33600A':
            rf_source.write("OUTP1 ON")

def sources_output_off():
    if source_mode == 'shared':
        ac_source.write("OUTP1 OFF")
        ac_source.write("OUTP2 OFF")
    else:
        if ac_source_model == '33600A':
            ac_source.write("OUTP1 OFF")
        elif ac_source_model == '5700A':
            ac_source.write("STBY")
        if rf_source_model == '33600A':
            rf_source.write("OUTP1 OFF")

# função instrument_init()
# inicializa a comunicação com os instrumentos, via GPIB
def instrument_init():
    # variáveis globais
    global ac_source;
    global rf_source;
    global std;
    global dut;
    global sw;

    if source_mode == 'shared':
        source_address = config['GPIB'].get('ac_source', config['GPIB'].get('rf_source', '5'))
        print("Comunicando com fonte AC/RF no endereço "+source_address+"...")
        ac_source = rm.open_resource("GPIB0::"+source_address+"::INSTR")
        rf_source = ac_source
        print(ac_source.query("*IDN?"))
        print("OK!\n")
    elif source_mode == 'separate':
        ac_address = config['GPIB'].get('ac_source', '5')
        rf_address = config['GPIB'].get('rf_source', ac_address)
        print("Comunicando com fonte AC no endereço "+ac_address+"...")
        ac_source = rm.open_resource("GPIB0::"+ac_address+"::INSTR")
        print(ac_source.query("*IDN?"))
        print("OK!\n")

        print("Comunicando com fonte RF no endereço "+rf_address+"...")
        rf_source = rm.open_resource("GPIB0::"+rf_address+"::INSTR")
        print(rf_source.query("*IDN?"))
        print("OK!\n")
    else:
        raise NameError("Valor inválido para Sources/mode (use shared ou separate).")

    print("Comunicando com o medidor do padrão no endereço "+config['GPIB']['std']+"...");
    std = rm.open_resource("GPIB0::"+config['GPIB']['std']+"::INSTR");
    configure_voltmeter(std, std_model)

    print("Comunicando com o medidor do objeto no endereço "+config['GPIB']['dut']+"...");
    dut = rm.open_resource("GPIB0::"+config['GPIB']['dut']+"::INSTR");
    configure_voltmeter(dut, dut_model)
 
    print("Comunicando com a chave no endereço "+config['GPIB']['sw']+"...");
    sw = rm.open_resource("GPIB0::"+config['GPIB']['sw']+"::INSTR");
    sw.write_raw(reset);
    print("OK!\n");

    return
#-------------------------------------------------------------------------------
# comandos Agilent 33600A
# OUTP1:LOAD INF
# SOUR1:FUNC SIN
# SOUR1:VOLT 1.0 VRMS
# SOUR1:FREQ +1.0E+05
# OUTP1 ON

# função meas_init()
# inicializa os instrumentos, coloca as fontes em OPERATE, etc.
def meas_init():
    if source_mode == 'shared':
        ac_source.write("*RST")
        ac_source.write("*CLS")
        ac_source.write("OUTP1:LOAD "+load)
        ac_source.write("OUTP2:LOAD "+load)
        ac_source.write("SOUR1:FUNC SIN")
        ac_source.write("SOUR2:FUNC SIN")
    else:
        if ac_source_model == '33600A':
            ac_source.write("*RST")
            ac_source.write("*CLS")
            ac_source.write("OUTP1:LOAD "+load)
            ac_source.write("SOUR1:FUNC SIN")
        elif ac_source_model == '5700A':
            ac_source.write("*RST")
            ac_source.write("*CLS")

        if rf_source_model == '33600A':
            rf_source.write("*RST")
            rf_source.write("*CLS")
            rf_source.write("OUTP1:LOAD "+load)
            rf_source.write("SOUR1:FUNC SIN")

    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    set_rf_voltage_and_frequency(vac_nominal, 1000000)
    # Entrar em OPERATE
    espera(2); # esperar 2 segundos
    sources_output_on()
    espera(10);
    sw.write_raw(ac);
    espera(10);
    return
#-------------------------------------------------------------------------------
# função ler_std()
# retorna uma leitura single-shot da saída do TC padrão
# não aceita parâmetros de entrada
def ler_std():
    return read_voltmeter(std, std_model)
#-------------------------------------------------------------------------------
# função ler_std()
# retorna uma leitura single-shot da saída do TC objeto
# não aceita parâmetros de entrada
def ler_dut():
    return read_voltmeter(dut, dut_model)
#-------------------------------------------------------------------------------
# aceita como parâmetro o vetor com as leituras do padrão
# escreve na tela a última leitura da saída do TC padrão
def print_std(std_readings):
    return
#-------------------------------------------------------------------------------
# aceita como parâmetro o vetor com as leituras do objeto
# escreve na tela a última leitura da saída do TC objeto
def print_dut(dut_readings):
    return
#-------------------------------------------------------------------------------
# função aquecimento()
# aceita como parâmetro o tempo de aquecimento, em segundos
def aquecimento(tempo):
    # executa o aquecimento, mantendo a tensão nominal aplicada pelo tempo
    # (em segundos) definido na variavel "tempo"
    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    sw.write_raw(dc);
    espera(tempo);
    return
#-------------------------------------------------------------------------------
# função n_measure()
# aceita o número de repetições como parâmetro de entrada
# número de repetições DEVE ser par!
# se não for, será executada uma repetição a mais. p. ex.: 3 -> 4
# executa a medição do coeficiente de linearidade "n" do padrão e do objeto
# o algoritmo consiste em aplicar a tensão nominal, a tensão nominal + 1% e
# a tensão nominal -1%, registrando os respectivos valores de saída de padrão
# e objeto
def n_measure(M):
    # testa se M é par, se não for, soma 1 para se tornar par
    if int(M) % 2 != 0:
        M += 1;
    # define as variáveis que armazenam as leituras do padrão e do objeto
    std_readings = []
    dut_readings = []
    # variavel da constante V0 / (Vi-V0)
    k = []
    # aplica o valor nominal de tensão
    set_rf_voltage_and_frequency(vac_nominal, freq)
    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    
    espera(2); # espera 2 segundos
    sw.write_raw(dc);
    print("Vdc nominal: +{:.3f} V".format(vdc_nominal))
    # aguarda pelo tempo de espera configurado
    espera(wait_time);
    # lê as saídas de padrão e objeto, e armazena na variável std_readings e
    # dut_readings
    std_readings.append(ler_std())
    #espera(1)
    dut_readings.append(ler_dut())
    print_std(std_readings);
    print_dut(dut_readings);

    for i in range(1,M+1):
        # determina se i é par ou ímpar
        # se i é impar, v_i = 1,01*vdc_nominal
        # se i é par, v_i = 0,99*vdc_nominal
        if int(i) % 2 == 0:
            Vi = 0.99*vdc_nominal;
            k.append(-100);
        else:
            Vi = 1.01*vdc_nominal;
            k.append(100);

        sw.write_raw(ac);
        espera(2); # esperar 2 segundos
        set_ac_voltage_and_frequency(Vi, 100000)
        espera(2); # esperar 2 segundos
        sw.write_raw(dc);
        print("Vdc nominal + 1%: +{:.3f} V".format(Vi));
        # aguarda pelo tempo de espera configurado
        espera(wait_time);
        # lê as saídas de padrão e objeto, e armazena na variável std_readings e
        # dut_readings
        std_readings.append(ler_std())
        #espera(1)
        dut_readings.append(ler_dut())
        print_std(std_readings);
        print_dut(dut_readings);

    # cálculo do n
    sw.write_raw(ac); # mantém chave em ac durante cálculo

    X0 = float(std_readings[0].strip())

    Y0 = float(dut_readings[0].strip())
        
    del std_readings[0]
    del dut_readings[0]

    Xi = numpy.array([float(a.strip()) for a in std_readings]);

    Yi = numpy.array([float(a.strip()) for a in dut_readings]);

    nX = (Xi/X0 - 1) * k;
    nY = (Yi/Y0 - 1) * k;

    results = [numpy.mean(nX), numpy.std(nX, ddof=1), numpy.mean(nY), numpy.std(nY, ddof=1)];

    # retorna uma lista com vários arrays
    # o array results contém os resultados (média e desvio padrão de nX e nY)
    return {'results':results, 'Xi':Xi, 'X0':X0, 'Yi':Yi, 'Y0':Y0, 'k':k, 'nX':nX, 'nY':nY}
    
#-------------------------------------------------------------------------------
# função measure(vdc_atual, vac_atual, ciclo_ac)
# Executa os ciclos de medição, na sequência AC, +DC, AC, -DC e AC.
# aceita como parâmetros de entrada:
# vdc_atual - valor atual da tensão DC
# vac_atual - valor atual da tensão AC
# ciclo_ac - valor das leituras do último ciclo AC da medida anterior
# se não for a primeira medição, o primeiro ciclo AC aproveita as leituras do
# último ciclo AC da medição anterior
def measure(vdc_atual,vac_atual,ciclo_ac):
    # inicializa arrays de resultados
    std_readings = []
    dut_readings = []
    # configuração da fonte AC (RF)
    set_rf_voltage_and_frequency(vac_atual, freq)
    # configuração da fonte DC (AC 100 kHz)
    set_ac_voltage_and_frequency(vdc_atual, 100000)
    # Iniciar medição
    espera(2); # esperar 2 segundos

    if ui is not None:
        if ciclo_ac == []:
            ui.start_cycle_table()
        else:
            ui.start_cycle_table(
                float(str(ciclo_ac[0]).strip()) * 1000,
                float(str(ciclo_ac[1]).strip()) * 1000,
            )

    for i, cycle_type in enumerate(cycle_sequence):
        cycle_name = "RF" if cycle_type == 'RF' else "AC 100 kHz"
        if i == 0 and (ciclo_ac != []):
            print("Ciclo {}".format(cycle_name))
            if ui is not None:
                ui.set_status("Executando ciclo: {}".format(cycle_name))
            std_readings.append(ciclo_ac[0])
            dut_readings.append(ciclo_ac[1])
            if ui is not None:
                ui.add_cycle_reading(i, float(str(ciclo_ac[0]).strip()) * 1000, float(str(ciclo_ac[1]).strip()) * 1000)
            print_std(std_readings)
            print_dut(dut_readings)
            continue

        if cycle_type == 'RF':
            sw.write_raw(ac)
            print("Ciclo RF")
        else:
            sw.write_raw(dc)
            print("Ciclo AC 100 kHz")

        if ui is not None:
            ui.set_status("Executando ciclo: {}".format(cycle_name))

        espera(wait_time)
        std_readings.append(ler_std())
        dut_readings.append(ler_dut())
        if ui is not None:
            ui.add_cycle_reading(i, float(std_readings[-1].strip()) * 1000, float(dut_readings[-1].strip()) * 1000)
        print_std(std_readings)
        print_dut(dut_readings)

    # retorna as leituras obtidas para o objeto e para o padrão
    return {'std_readings':std_readings, 'dut_readings':dut_readings}
#-------------------------------------------------------------------------------
# função acdc_calc(readings,N,vdc_atual)
# Calcula a diferença RF-AC a partir dos dados obtidos com a funcao measure()
# aceita como parâmetros de entrada:
# readings - array com as leituras obtidas para o padrão e para o objeto
# N - vetor com os valores calculados de N (padrão e objeto)
# vdc_atual - valor de tensão DC ajustado para o último ciclo.
def acdc_calc(readings,N,vdc_atual):
    # x -> padrao; y -> objeto
    print("Calculando diferença RF-AC...")
    n_X = N[0]; # n do padrão
    n_Y = N[2]; # n do objeto
    # extrai os dados de leituras do padrão
    x = numpy.array([float(a.strip()) for a in readings['std_readings']]);
    # extrai os dados de leitura do objeto
    y = numpy.array([float(a.strip()) for a in readings['dut_readings']])
    # calcula Xac, Xdc, Yac e Ydc a partir das leituras brutas
    Xac = numpy.mean(x[rf_indices]);
    Xdc = numpy.mean(x[ac_indices]);
    Yac = numpy.mean(y[rf_indices]);
    Ydc = numpy.mean(y[ac_indices]);
    # Variáveis auxiliares X e Y
    X = Xac/Xdc - 1;
    Y = Yac/Ydc - 1;
    # diferença RF-AC medida:
    # denominador (1 + Y/n_Y) removido; sugestao H. Laiz durante peer review.
    delta_m = 1e6 * (X/n_X - Y/n_Y);
    # critério para repetir a medição - diferença entre Yac e Ydc    
    Delta = 1e6 * ((Yac - Ydc)/Ydc);
    # ajuste da tensão DC para o próximo ciclo
    adj_dc = vdc_atual * (1 + (Yac - Ydc)/(n_Y * Ydc));
    # timestamp de cada medição
    date = datetime.datetime.now();
    timestamp = datetime.datetime.strftime(date, '%d/%m/%Y %H:%M:%S');
    # retorna lista com os arrays de leitura do padrão, objeto, a diferença ac-dc,
    # Delta=Yac-Ydc, o ajuste DC e o horário
    return {'std_readings':x,'dut_readings':y,'dif':delta_m, 'Delta':Delta, 'adj_dc':adj_dc,'timestamp':timestamp}
#-------------------------------------------------------------------------------
# função equilibrio()
# Calcula a tensão de equilíbrio AC no início da sequência de medições
# A função não aceita parâmetros de entrada
def equilibrio():
    dut_readings = []
    set_rf_voltage_and_frequency(vac_nominal, freq)
    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    espera(5) # aguarda 5 segundos antes de iniciar equilibrio
        
    # Aplica o valor nominal
    sw.write_raw(dc);
    print("Vdc nominal: +{:.3f} V".format(vdc_nominal))
    espera(wait_time/2);
    set_rf_voltage_and_frequency(0.999*vac_nominal, freq)
    espera(wait_time/2);
    dut_readings.append(ler_dut())
    print_dut(dut_readings);
    # Aplica Vac - 0.1%
    print("Vac nominal - 0.1%: +{:.3f} V".format(0.999*vac_nominal))
    sw.write_raw(ac);
    espera(wait_time)
    dut_readings.append(ler_dut())
    print_dut(dut_readings);
    sw.write_raw(dc);
    espera(2);
    set_rf_voltage_and_frequency(1.001*vac_nominal, freq)
    espera(2);
    # Aplica Vac + 0.1%
    print("Vac nominal + 0.1%: +{:.3f} V".format(1.001*vac_nominal))
    sw.write_raw(ac);
    espera(wait_time)
    dut_readings.append(ler_dut())
    print_dut(dut_readings);
    sw.write_raw(dc);
    # cálculo do equilíbrio
    yp = [0.999*vac_nominal, 1.001*vac_nominal]
    
    xp = [float(dut_readings[1].strip()), float(dut_readings[2].strip())]
    xi = float(dut_readings[0].strip())
    # calcula o valor de equilíbrio através de interpolação linear    
    new_ac = numpy.interp(xi,xp,yp);
    # retorna o novo valor de AC
    return new_ac
#-------------------------------------------------------------------------------
# função stop_instruments()
# função chamada para interromper a medição
# não aceita parâmetros de entrada
# coloca as fontes em stand-by
def stop_instruments():
    sw.write_raw(reset);
    espera(1)
    sources_output_off()
    return
#-------------------------------------------------------------------------------
# função criar_registro()
# Cria um novo registro de medição
# Não aceita parâmetros de entrada
def criar_registro():
    date = datetime.datetime.now();
    timestamp_file = datetime.datetime.strftime(date, '%d-%m-%Y_%Hh%Mm');
    timestamp_registro = datetime.datetime.strftime(date, '%d/%m/%Y %H:%M:%S');
    # o nome do registro é criado de forma automática, a partir da data e hora atuais
    registro_filename = "leituras/registro_"+timestamp_file+".csv"
    with open(registro_filename,"w") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        registro.writerow(['pyAC-DC '+versao]);
        registro.writerow(['Registro de Medições']);
        registro.writerow([' ']);
        registro.writerow(['Início da medição',timestamp_registro]);
        registro.writerow(['Tempo de aquecimento [s]',config['Measurement Config']['aquecimento']]);
        registro.writerow(['Tempo de estabilização [s]',config['Measurement Config']['wait_time']]);
        registro.writerow(['Repetições',config['Measurement Config']['repeticoes']]);
        registro.writerow(['Observações',config['Misc']['observacoes']]);
        registro.writerow([' ']);
        registro.writerow([' ']);

    csvfile.close();
    return registro_filename
#-------------------------------------------------------------------------------
# função registro_frequencia(egistro_filename,frequencia,n_value,vac_equilibrio)
# Inicia uma nova frequência no registro de medição
# Aceita os parâmetros
# registro_filename - o nome do registro criado com a função criar_registro()
# frequencia - o valor da frequência que está sendo medida no momento;
# n_value - os valores obtidos de n para padrão e objeto
# vac_equilibrio - a tensão AC de equilíbrio calculada com a funcao equilibrio()
# n_array:
# {'results':results, 'Xi':Xi, 'X0':X0, 'Yi':Yi, 'Y0':Y0, 'k':k, 'nX':nX, 'nY':nY}
def registro_frequencia(registro_filename,frequencia,n_array,vac_equilibrio):
    with open(registro_filename,"a") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        registro.writerow(['Tensão [V]',config['Measurement Config']['voltage'].replace('.',',')]);
        registro.writerow(['Frequência [MHz]',frequencia.replace('.',',')]);
        registro.writerow([' ']); # pular linha
        registro.writerow(['X0',str(n_array['X0']).replace('.',',')]); # valor de X0
        registro.writerow(['Xi'] + [str(i).replace('.',',') for i in n_array['Xi']]); # valores de Xi
        registro.writerow(['k'] + [str(i).replace('.',',') for i in n_array['k']]); # valores de k
        registro.writerow(['nX'] + [str(i).replace('.',',') for i in n_array['nX']]); # valores de nX
        registro.writerow(['nX (média)',str(n_array['results'][0]).replace('.',',')]); # Valor médio de nX
        registro.writerow(['nX (desvio padrão)',str(n_array['results'][1]).replace('.',',')]); # desvio padrão de nX
        registro.writerow([' ']); # pular linha
        registro.writerow(['Y0',str(n_array['Y0']).replace('.',',')]); # valor de X0
        registro.writerow(['Yi'] + [str(i).replace('.',',') for i in n_array['Yi']]); # valores de Yi
        registro.writerow(['k'] + [str(i).replace('.',',') for i in n_array['k']]); # valores de k
        registro.writerow(['nY'] + [str(i).replace('.',',') for i in n_array['nY']]); # valores de nY
        registro.writerow(['nY (média)',str(n_array['results'][2]).replace('.',',')]); # valor médio de nY
        registro.writerow(['nY (desvio padrão)',str(n_array['results'][3]).replace('.',',')]); # desvio padrão de nY
        registro.writerow([' ']); # pular linha
        registro.writerow(['Vac equilíbrio [V]',str(vac_equilibrio).replace('.',',')]); # Vac calculado para o equilíbrio
        registro.writerow([' ']); # pular linha
        # cabeçalho da tabela de medicao
        header = ['Data / hora']
        for label in cycle_csv_labels:
            header.extend([label+' (STD)', label+' (DUT)'])
        header.extend(['Diferença', 'Delta', 'Tensão DC Aplicada'])
        if use_bme280:
            header.extend(['Temperatura [ºC]', 'Umidade Relativa [% u.r.]', 'Pressão Atmosférica [hPa]'])
        registro.writerow(header)

    csvfile.close();
    return
#-------------------------------------------------------------------------------
# função registro_linha(registro_filename,results,vdc_atual)
# salva uma nova linha (medição individual) no registro de medição
# parâmetros:
# registro_filename - o nome do registro criado com a função criar_registro()
# results - array com os resultados
# vdc_atual - tensão DC calculada para a medição atual
#def registro_linha(registro_filename,results,vdc_atual,ca_data):
def registro_linha(registro_filename,results,vdc_atual,ca_data=None):

    # results -> results['std_readings'], results['dut_readings'], results['dif'], results['Delta'], results['adj_dc'] e results['timestamp']
    with open(registro_filename,"a") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        row = [results['timestamp']]
        for std_value, dut_value in zip(results['std_readings'], results['dut_readings']):
            row.extend([str(std_value).replace('.',','), str(dut_value).replace('.',',')])
        row.extend([str(results['dif']).replace('.',','),str(results['Delta']).replace('.',','),str(vdc_atual).replace('.',',')])
        if use_bme280 and ca_data is not None:
            row.extend([str(ca_data.temperature).replace('.',','),str(ca_data.humidity).replace('.',','),str(ca_data.pressure).replace('.',',')])
        registro.writerow(row)


    csvfile.close();
    return
#-------------------------------------------------------------------------------
# função registro_media(registro_filename,diferenca):
# finaliza o registro de medição para cada frequência, escrevendo a média
# e desvio padrão obtidos.
# Aceita os parâmetros:
# registro_filename - o nome do registro criado com a função criar_registro()
# diferenca - array com a média e o desvio padrão calculados
def registro_media(registro_filename,diferenca):
    with open(registro_filename,"a") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        registro.writerow([' ']);
        registro.writerow(['Média',str(numpy.mean(diferenca)).replace('.',',')]);
        registro.writerow(['Desvio-padrão',str(numpy.std(diferenca, ddof=1)).replace('.',',')]);
        registro.writerow([' ']);
        registro.writerow([' ']);
    csvfile.close();
    return
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Programa principal
#-------------------------------------------------------------------------------
def main():
    global ui
    try:
        global freq;
        ui = MeasurementUI()
        ui.start()
        ui.set_status("Inicializando sistema")
        ui.set_program([float(v.strip()) for v in freq_array], vdc_nominal, vac_nominal)
        ui.set_repetition(0, repeticoes)
        if use_bme280:
            ui.set_status("Inicializando BME280 (condições ambientais)")
            bme280_init()
        ui.set_status("Inicializando instrumentos")
        instrument_init()  # inicializa os instrumentos
        ui.set_status("Colocando fontes em OPERATE")
        meas_init()        # inicializa a medição (coloca fontes em operate)
        ui.set_status("Criando arquivo de registro")
        filename = criar_registro();  # cria arquivo de registro
        ui.set_status("Arquivo {} criado".format(filename))
        ui.set_status("Aquecimento")
        aquecimento(heating_time);  # inicia o aquecimento
        # fazer loop para cada valor de frequencia
        for value in freq_array:
            freq = float(value) * 1000000;
            ui.set_frequency(freq/1e6)
            ui.set_status("Iniciando medicao em {:5.0f} MHz".format(freq/1e6))
            ui.set_status("Medindo N")
            n_array = n_measure(4);  # 4 repetições para o cálculo do N
            n_value = n_array['results'];
            ui.set_n_values(n_value[0], n_value[2])
            ui.set_status("N STD {:.2f} (dp {:.2f}) | N DUT {:.2f} (dp {:.2f})".format(n_value[0], n_value[1], n_value[2], n_value[3]))
            ui.set_status("Calculando equilibrio AC")
            vac_atual = equilibrio();  # calcula a tensão AC de equilíbrio
            ui.set_status("Vac aplicado: {:5.3f} V".format(vac_atual))
            ui.set_setpoints(vdc_nominal, vac_atual)
            registro_frequencia(filename,value,n_array,vac_atual);  # inicia o registro para a frequencia atual
            first_measure = True;   # flag para determinar se é a primeira repeticao
            reuse_last_cycle = True

            if vac_atual > 1.1*vac_nominal:  # verifica se a tensão AC de equilíbrio não é muito elevada
                raise NameError('Tensão AC ajustada perigosamente alta!')
            
            ui.set_status("Iniciando repeticoes da medicao")
            diff_acdc = [];
            Delta = [];
            vdc_atual = vdc_nominal;
            i = 0;
            while (i < repeticoes):  # inicia as repetições da medição
                ui.set_status("Repeticao {}/{} | Vdc {:5.3f} V".format(i+1, repeticoes, vdc_atual))
                ui.set_setpoints(vdc_atual, vac_atual)
                ui.set_repetition(i+1, repeticoes)
                if first_measure:    # testa se é a primeira medição
                    ciclo_ac = [];
                    first_measure = False
                else:
                    if reuse_last_cycle:
                        ciclo_ac = [readings['std_readings'][-1], readings['dut_readings'][-1]];  # caso não seja, aproveitar o último ciclo
                    else:
                        ciclo_ac = []
                readings = measure(vdc_atual,vac_atual,ciclo_ac);                           # da repetição anterior
                results = acdc_calc(readings,n_value,vdc_atual);                            # calcula a diferença ac-dc         
                ca_data = None
                if use_bme280:
                    ca_data = bme280_read();
                # original: 50 ppm
                # usando gerador agilent: 1000 ppm (ou 0,1%) (estabilidade e resolucao nao permite criterio tao  rigido)
                if abs(results['Delta']) > delta_max_ppm:               # se o ponto não passa no critério de descarte, repetir medição
                    ui.add_result(results['dif'], results['Delta'], True)
                    ui.set_status("Ponto descartado: Delta {:.2f} µV/V > {:.1f} µV/V".format(results['Delta'], delta_max_ppm))
                    reuse_last_cycle = (measurement_cycle != 'AC-RF-AC')
                else:
                    ui.add_result(results['dif'], results['Delta'], False)
                    diff_acdc.append(results['dif']);
                    Delta.append(results['Delta']);
                    registro_linha(filename,results,vdc_atual,ca_data);
                    reuse_last_cycle = True

                    i += 1;               
                vdc_atual = results['adj_dc'];              # aplica o ajuste DC
                if vdc_atual > 1.1*vdc_nominal:
                    raise NameError('Tensão DC ajustada perigosamente alta!')    

            freq_mean = numpy.mean(diff_acdc)
            freq_std = numpy.std(diff_acdc, ddof=1)
            ui.add_frequency_summary(freq/1e6, freq_mean, freq_std)
            ui.set_status("Medição concluída | Média {:.2f} µV/V | DP {:.2f} µV/V".format(freq_mean, freq_std))
            registro_media(filename,diff_acdc);             # salva a diferença ac-dc média para a frequência atual no registro

        stop_instruments();                                 # coloca as fontes em stand-by
        ui.set_status("Concluído")
        time.sleep(1)
                
    except:
        try:
            stop_instruments()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
    finally:
        if ui is not None:
            ui.stop()
        

# execução do programa principal
if __name__ == '__main__':
    main()
