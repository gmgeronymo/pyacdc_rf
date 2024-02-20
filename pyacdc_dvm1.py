# pyAC-DC.py
# Programa para a medição de diferença AC-DC em conversores térmicos (TCs)
# O programa aceita TCs com saída em tensão, frequência e resistência.
# modificado em outubro de 2023 para calibrar TVCs Fluke A55 acima de 1 MHz
# usando gerador Keysight 33600A como fonte (AC e RF)
# usando Keithley 2182A como detector (ch1 -> dut; ch2 -> std)
#-------------------------------------------------------------------------------
# Autor:       Gean Marcos Geronymo
#
# Versão inicial:      10-Jun-2016
# Última modificação:  18-Out-2023
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
# Na versao RF-DC eh usado um unico instrumento -> dvm
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
import datetime
import csv
# condicoes ambientais - bme280
import smbus2
import bme280
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
load = str(int(1 / ( (1/r_dut) + (1/r_std) )))

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
    for i in range(int(segundos * 10)):
        time.sleep(0.1)    
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

# função instrument_init()
# inicializa a comunicação com os instrumentos, via GPIB
def instrument_init():
    # variáveis globais
    global ac_source;
    global dvm;
    global sw;
    # Inicialização dos intrumentos conectados ao barramento GPIB
    print("Comunicando com fonte AC no endereço "+config['GPIB']['ac_source']+"...");
    ac_source = rm.open_resource("GPIB0::"+config['GPIB']['ac_source']+"::INSTR");
    print(ac_source.query("*IDN?"));
    print("OK!\n");

    print("Comunicando com o DVM no endereço "+config['GPIB']['dvm']+"...");
    dvm = rm.open_resource("GPIB0::"+config['GPIB']['dvm']+"::INSTR");
  
    # configura ch1 (std)
    dvm.write(":SENS:VOLT:NPLC 18")
    dvm.write(":SENS:VOLT:DIG 8")
 
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
    # configuração da fonte AC
    # canal 1: ac 100 kHz
    # canal 2: rf (default 1 MHz)
    ac_source.write("*RST")
    ac_source.write("*CLS")
    ac_source.write("OUTP1:LOAD "+load)
    ac_source.write("OUTP2:LOAD "+load)
    ac_source.write("SOUR1:FUNC SIN")
    ac_source.write("SOUR2:FUNC SIN")
    ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(vdc_nominal));
    ac_source.write("SOUR2:VOLT {:.3f} VRMS".format(vac_nominal));
    ac_source.write("SOUR1:FREQ 100000")
    ac_source.write("SOUR2:FREQ 1000000")
    # Entrar em OPERATE
    espera(2); # esperar 2 segundos
    ac_source.write("OUTP1 ON");
    ac_source.write("OUTP2 ON");
    espera(10);
    sw.write_raw(ac);
    espera(10);
    return
#-------------------------------------------------------------------------------
# função ler_std()
# retorna uma leitura single-shot da saída do TC padrão
# não aceita parâmetros de entrada
def ler_std():
    dvm.write("SENS:CHAN 2")
    dvm.write(":SENS:VOLT:CHAN2:RANG 0.01")
    dvm.write("INIT:CONT OFF")
    x = dvm.query(":READ?")
    return x
#-------------------------------------------------------------------------------
# função ler_std()
# retorna uma leitura single-shot da saída do TC objeto
# não aceita parâmetros de entrada
def ler_dut():
    dvm.write("SENS:CHAN 1")
    dvm.write(":SENS:VOLT:CHAN1:RANG 0.01")
    dvm.write("INIT:CONT OFF")
    x = dvm.query(":READ?")
    return x
#-------------------------------------------------------------------------------
# função ler_std()
# aceita como parâmetro o vetor com as leituras do padrão
# escreve na tela a última leitura da saída do TC padrão
def print_std(std_readings):
    print("STD [mV] {:5.6f}".format(float(std_readings[-1].strip())*1000))
    return
#-------------------------------------------------------------------------------
# função ler_std()
# aceita como parâmetro o vetor com as leituras do objeto
# escreve na tela a última leitura da saída do TC objeto
def print_dut(dut_readings):
    print("DUT [mV] {:5.6f}".format(float(dut_readings[-1].strip())*1000))
    return
#-------------------------------------------------------------------------------
# função aquecimento()
# aceita como parâmetro o tempo de aquecimento, em segundos
def aquecimento(tempo):
    # executa o aquecimento, mantendo a tensão nominal aplicada pelo tempo
    # (em segundos) definido na variavel "tempo"
    ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(vdc_nominal));
    ac_source.write("SOUR1:FREQ +1.0E+05");
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
    #ac_source.write("OUT {:.6f} V".format(vac_nominal));
    ac_source.write("SOUR2:VOLT {:.3f} VRMS".format(vac_nominal));
    #ac_source.write("OUT "+str(freq)+" HZ");
    ac_source.write("SOUR2:FREQ "+str(freq));
    #dc_source.write("OUT +{:.6f} V".format(vdc_nominal));
    ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(vdc_nominal));
    
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
        #dc_source.write("OUT +{:.6f} V".format(Vi));
        ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(Vi));
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
    #ac_source.write("OUT {:.6f} V".format(vac_atual));
    ac_source.write("SOUR2:VOLT {:.3f} VRMS".format(vac_atual));
    #ac_source.write("OUT "+str(freq)+" HZ");
    ac_source.write("SOUR2:FREQ "+str(freq));
    # configuração da fonte DC (AC 100 kHz)
    #dc_source.write("OUT +{:.6f} V".format(vdc_atual));
    ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(vdc_atual));
    #dc_source.write("OUT 0 HZ");
    ac_source.write("SOUR1:FREQ 100000");
    # Iniciar medição
    espera(2); # esperar 2 segundos
    # Ciclo AC
    # testa se existem dados do último ciclo AC da medição anterior
    if (ciclo_ac == []):
        # caso negativo, medir AC normalmente
        sw.write_raw(ac);
        print("Ciclo RF")
        espera(wait_time);
        # leituras
        std_readings.append(ler_std())
        #espera(1)
        dut_readings.append(ler_dut())
        print_std(std_readings);
        print_dut(dut_readings);
    else:
        # caso positivo, aproveitar as medições do ciclo anterior
        print("Ciclo RF")
        std_readings.append(ciclo_ac[0])
        dut_readings.append(ciclo_ac[1])
        print_std(std_readings);
        print_dut(dut_readings);
    # Ciclo DC
    sw.write_raw(dc);
    print("Ciclo AC 100 kHz")
    espera(wait_time);
    std_readings.append(ler_std())
    #espera(1)
    dut_readings.append(ler_dut())
    print_std(std_readings);
    print_dut(dut_readings);
    # Ciclo AC
    sw.write_raw(ac);
    print("Ciclo RF")
    #espera(wait_time/2);
    # Mudar fonte DC para -DC
    #dc_source.write("OUT -{:.6f} V".format(vdc_atual));
    #espera(wait_time/2);
    espera(wait_time);
    std_readings.append(ler_std())
    #espera(1)
    dut_readings.append(ler_dut())
    print_std(std_readings);
    print_dut(dut_readings);
    # Ciclo -DC
    sw.write_raw(dc);
    print("Ciclo AC 100 kHz")
    espera(wait_time);
    std_readings.append(ler_std())
    #espera(1)
    dut_readings.append(ler_dut())
    print_std(std_readings);
    print_dut(dut_readings);
    # Ciclo AC
    sw.write_raw(ac);
    print("Ciclo RF")
    #espera(wait_time/2);
    # Mudar fonte DC para +DC
    #dc_source.write("OUT +{:.6f} V".format(vdc_atual));
    #espera(wait_time/2);
    espera(wait_time);
    std_readings.append(ler_std())
    #espera(1)
    dut_readings.append(ler_dut())
    print_std(std_readings);
    print_dut(dut_readings);
    # retorna as leituras obtidas para o objeto e para o padrão
    return {'std_readings':std_readings, 'dut_readings':dut_readings}
#-------------------------------------------------------------------------------
# função acdc_calc(readings,N,vdc_atual)
# Calcula a diferença AC-DC a partir dos dados obtidos com a funcao measure()
# aceita como parâmetros de entrada:
# readings - array com as leituras obtidas para o padrão e para o objeto
# N - vetor com os valores calculados de N (padrão e objeto)
# vdc_atual - valor de tensão DC ajustado para o último ciclo.
def acdc_calc(readings,N,vdc_atual):
    # x -> padrao; y -> objeto
    print("Calculando diferença ac-dc...")
    n_X = N[0]; # n do padrão
    n_Y = N[2]; # n do objeto
    # extrai os dados de leituras do padrão
    x = numpy.array([float(a.strip()) for a in readings['std_readings']]);
    # extrai os dados de leitura do objeto
    y = numpy.array([float(a.strip()) for a in readings['dut_readings']])
    # calcula Xac, Xdc, Yac e Ydc a partir das leituras brutas    
    Xac = numpy.mean(numpy.array([x[0], x[2], x[4]]));     # AC médio padrão
    Xdc = numpy.mean(numpy.array([x[1], x[3]]));           # DC médio padrão
    Yac = numpy.mean(numpy.array([y[0], y[2], y[4]]));     # AC médio objeto
    Ydc = numpy.mean(numpy.array([y[1], y[3]]));           # DC médio objeto
    # Variáveis auxiliares X e Y
    X = Xac/Xdc - 1;
    Y = Yac/Ydc - 1;
    # diferença AC-DC medida:
    delta_m = 1e6 * ((X/n_X - Y/n_Y)/(1 + Y/n_Y));
    # critério para repetir a medição - diferença entre Yac e Ydc    
    Delta = 1e6 * (Yac - Ydc);
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
    #ac_source.write("OUT "+str(freq)+" HZ");
    ac_source.write("SOUR2:FREQ "+str(freq));
    #dc_source.write("OUT {:.6f} V".format(vdc_nominal));
    ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(vdc_nominal));
    espera(5) # aguarda 5 segundos antes de iniciar equilibrio
        
    # Aplica o valor nominal
    sw.write_raw(dc);
    print("Vdc nominal: +{:.3f} V".format(vdc_nominal))
    espera(wait_time/2);
    #ac_source.write("OUT {:.6f} V".format(0.999*vac_nominal));
    ac_source.write("SOUR2:VOLT {:.3f} VRMS".format(0.999*vac_nominal));
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
    #ac_source.write("OUT {:.6f} V".format(1.001*vac_nominal));
    ac_source.write("SOUR2:VOLT {:.3f} VRMS".format(1.001*vac_nominal));
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
    #ac_source.write("STBY");
    #dc_source.write("STBY");
    ac_source.write("OUTP1 OFF")
    ac_source.write("OUTP2 OFF")
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
        registro.writerow(['Frequência [kHz]',frequencia.replace('.',',')]);
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
        registro.writerow(['Data / hora','AC (STD)','AC (DUT)','DC+ (STD)','DC+ (DUT)','AC (STD)','AC (DUT)','DC- (STD)','DC- (DUT)','AC (STD)','AC (DUT)', 'Diferença', 'Delta', 'Tensão DC Aplicada','Temperatura [ºC]', 'Umidade Relativa [% u.r.]', 'Pressão Atmosférica [hPa]']);
    csvfile.close();
    return
#-------------------------------------------------------------------------------
# função registro_linha(registro_filename,results,vdc_atual)
# salva uma nova linha (medição individual) no registro de medição
# parâmetros:
# registro_filename - o nome do registro criado com a função criar_registro()
# results - array com os resultados
# vdc_atual - tensão DC calculada para a medição atual
def registro_linha(registro_filename,results,vdc_atual,ca_data):
    # results -> results['std_readings'], results['dut_readings'], results['dif'], results['Delta'], results['adj_dc'] e results['timestamp']
    with open(registro_filename,"a") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        registro.writerow([results['timestamp'],str(results['std_readings'][0]).replace('.',','),str(results['dut_readings'][0]).replace('.',','),str(results['std_readings'][1]).replace('.',','),str(results['dut_readings'][1]).replace('.',','),str(results['std_readings'][2]).replace('.',','),str(results['dut_readings'][2]).replace('.',','),str(results['std_readings'][3]).replace('.',','),str(results['dut_readings'][3]).replace('.',','),str(results['std_readings'][4]).replace('.',','),str(results['dut_readings'][4]).replace('.',','),str(results['dif']).replace('.',','),str(results['Delta']).replace('.',','),str(vdc_atual).replace('.',','),str(ca_data.temperature).replace('.',','),str(ca_data.humidity).replace('.',','),str(ca_data.pressure).replace('.',',')]);

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
    try:
        global freq;
        print("Inicializando BME280 (condições ambientais)")
        bme280_init()
        print("Inicializando os intrumentos...")
        instrument_init()  # inicializa os instrumentos
        print("Colocando fontes em OPERATE...")
        meas_init()        # inicializa a medição (coloca fontes em operate)
        print("Criando arquivo de registro...")
        filename = criar_registro();  # cria arquivo de registro
        print("Arquivo "+filename+" criado com sucesso!")
        print("Aquecimento...");   
        aquecimento(heating_time);  # inicia o aquecimento
        # fazer loop para cada valor de frequencia
        for value in freq_array:
            freq = float(value) * 1000000;
            print("Iniciando a medição...")
            print("V nominal: {:5.2f} V, f nominal: {:5.2f} Hz".format(vdc_nominal,freq));
            print("Medindo o N...");           
            n_array = n_measure(4);  # 4 repetições para o cálculo do N
            n_value = n_array['results'];
            print("N STD (média): {:5.2f}".format(n_value[0]))
            print("N STD (desvio padrão): {:5.2f}".format(n_value[1]))
            print("N DUT (média): {:5.2f}".format(n_value[2]))
            print("N DUT (desvio padrão): {:5.2f}".format(n_value[3]))   
            print("Equilibrio AC...");
            vac_atual = equilibrio();  # calcula a tensão AC de equilíbrio
            print("Vac aplicado: {:5.3f} V".format(vac_atual))
            registro_frequencia(filename,value,n_array,vac_atual);  # inicia o registro para a frequencia atual
            first_measure = True;   # flag para determinar se é a primeira repeticao

            if vac_atual > 1.1*vac_nominal:  # verifica se a tensão AC de equilíbrio não é muito elevada
                raise NameError('Tensão AC ajustada perigosamente alta!')
            
            print("Iniciando medição...");
            diff_acdc = [];
            Delta = [];
            vdc_atual = vdc_nominal;
            i = 0;
            while (i < repeticoes):  # inicia as repetições da medição
                print ("Vdc aplicado: {:5.3f} V".format(vdc_atual))
                if first_measure:    # testa se é a primeira medição
                    ciclo_ac = [];
                    first_measure = False
                else:
                    ciclo_ac = [readings['std_readings'][4], readings['dut_readings'][4]];  # caso não seja, aproveitar o último ciclo AC
                readings = measure(vdc_atual,vac_atual,ciclo_ac);                           # da repetição anterior
                results = acdc_calc(readings,n_value,vdc_atual);                            # calcula a diferença ac-dc         
                print("Diferença ac-dc: {:5.2f}".format(results['dif']))               
                print("Delta: {:5.2f}".format(results['Delta']))
                print("Data / hora: "+results['timestamp']);
                ca_data = bme280_read();
                print("Temperatura: {:5.2f} ºC".format(ca_data.temperature));
                print("Umidade Relativa: {:5.2f} %u.r.".format(ca_data.humidity));
                print("Pressão atmosférica: {:5.2f} hPa".format(ca_data.pressure));
                if abs(results['Delta']) > 50:               # se o ponto não passa no critério de descarte, repetir medição
                    print("Delta > 50. Ponto descartado!")
                else:
                    diff_acdc.append(results['dif']);
                    Delta.append(results['Delta']);
                    registro_linha(filename,results,vdc_atual,ca_data);
                    i += 1;               
                vdc_atual = results['adj_dc'];              # aplica o ajuste DC
                if vdc_atual > 1.1*vdc_nominal:
                    raise NameError('Tensão DC ajustada perigosamente alta!')    

            print("Medição concluída.")                      
        
            print("Resultados:")
            print("Média: {:5.2f}".format(numpy.mean(diff_acdc)))
            print("Desvio padrão: {:5.2f}".format(numpy.std(diff_acdc, ddof=1)))
            print("Salvando arquivo...")
            registro_media(filename,diff_acdc);             # salva a diferença ac-dc média para a frequência atual no registro

        stop_instruments();                                 # coloca as fontes em stand-by
        print("Concluído.")
                
    except:
        stop_instruments()
        import traceback
        traceback.print_exc()
        

# execução do programa principal
if __name__ == '__main__':
    main()
