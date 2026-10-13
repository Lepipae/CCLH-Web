import { useCallback, useEffect, useRef, useState } from 'react'
import { soundManager } from '../utils/soundManager'
import { IMPORT_RULES, validateImportData } from '../utils/cardsValidation'

// Lógica con estado del taller: socket + fichero + alertas. El componente solo
// pinta; la validación pura vive en utils/cardsValidation (testeable).
// La verdad sigue en el servidor: aquí solo se guarda lo que el servidor
// devuelve en `custom_cards_list / custom_card_result / custom_cards_imported`.
export function useCustomCards(socket) {
  const [cardType, setCardType] = useState('white')
  const [cardText, setCardText] = useState('')
  const [cardPick, setCardPick] = useState(1)
  const [loading, setLoading] = useState(false)
  const [alertInfo, setAlertInfo] = useState(null) // { message, isError }
  const [customCards, setCustomCards] = useState({ whiteCards: [], blackCards: [] })
  const [filterTab, setFilterTab] = useState('all') // 'all' | 'white' | 'black'

  const fileInputRef = useRef(null)
  const [importing, setImporting] = useState(false)
  const [pendingImport, setPendingImport] = useState(null) // { fileName, data, check }
  const [importResult, setImportResult] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null) // { type, text } | null
  const alertTimer = useRef(null)

  const showAlert = useCallback((message, isError) => {
    setAlertInfo({ message, isError })
    if (alertTimer.current) clearTimeout(alertTimer.current)
    alertTimer.current = setTimeout(() => setAlertInfo(null), 4000)
  }, [])

  const fetchCustomCards = useCallback(() => {
    socket.emit('get_custom_cards')
  }, [socket])

  useEffect(() => {
    fetchCustomCards()

    const onList = (data) => {
      if (data.success) {
        setCustomCards({
          whiteCards: data.whiteCards || [],
          blackCards: data.blackCards || [],
        })
      }
    }
    const onResult = (data) => {
      setLoading(false)
      if (data.success) {
        showAlert('¡Carta guardada con éxito en cartasCustom.json!', false)
        setCardText('')
        if (data.whiteCards || data.blackCards) {
          setCustomCards({
            whiteCards: data.whiteCards || [],
            blackCards: data.blackCards || [],
          })
        } else {
          fetchCustomCards()
        }
      } else {
        showAlert('Error: ' + data.message, true)
      }
    }
    const onDeleted = (data) => {
      if (data.success) {
        setCustomCards({
          whiteCards: data.whiteCards || [],
          blackCards: data.blackCards || [],
        })
        showAlert('Carta eliminada de cartasCustom.json', false)
      } else {
        showAlert('Error eliminando carta: ' + data.message, true)
      }
      setConfirmDelete(null)
    }
    const onImported = (data) => {
      setImporting(false)
      setPendingImport(null)
      if (data.success) {
        setCustomCards({
          whiteCards: data.whiteCards || [],
          blackCards: data.blackCards || [],
        })
        setImportResult(data)
      } else {
        showAlert('Error importando: ' + (data.message || 'desconocido'), true)
      }
    }

    socket.on('custom_cards_list', onList)
    socket.on('custom_card_result', onResult)
    socket.on('custom_card_deleted', onDeleted)
    socket.on('custom_cards_imported', onImported)
    return () => {
      socket.off('custom_cards_list', onList)
      socket.off('custom_card_result', onResult)
      socket.off('custom_card_deleted', onDeleted)
      socket.off('custom_cards_imported', onImported)
      if (alertTimer.current) clearTimeout(alertTimer.current)
    }
  }, [socket, fetchCustomCards, showAlert])

  const handleAddCard = (e) => {
    e.preventDefault()
    if (!cardText.trim()) {
      showAlert('El texto de la carta no puede estar vacío.', true)
      return
    }
    soundManager.playCard()
    setLoading(true)
    socket.emit('add_custom_card', {
      type: cardType,
      text: cardText.trim(),
      pick: cardType === 'black' ? parseInt(cardPick, 10) || 1 : 1,
    })
  }

  // El borrado se confirma con modal propio (no window.confirm: bloquea el
  // hilo y los lectores de pantalla no lo anuncian).
  const requestDeleteCard = (type, text) => setConfirmDelete({ type, text })
  const cancelDeleteCard = () => setConfirmDelete(null)
  const confirmDeleteCard = () => {
    if (!confirmDelete) return
    socket.emit('delete_custom_card', { type: confirmDelete.type, text: confirmDelete.text })
  }

  const openFilePicker = () => {
    if (fileInputRef.current) fileInputRef.current.click()
  }

  const handleFileSelected = (e) => {
    const file = e.target.files && e.target.files[0]
    e.target.value = '' // permite re-seleccionar el mismo archivo
    if (!file) return

    if (file.size > IMPORT_RULES.maxFileBytes) {
      showAlert('El archivo es demasiado grande (máximo 5 MB).', true)
      return
    }

    const reader = new FileReader()
    reader.onload = () => {
      let parsed
      try {
        parsed = JSON.parse(reader.result)
      } catch (err) {
        showAlert(`"${file.name}" no es un JSON válido: ${err.message}`, true)
        return
      }
      // Preview con aviso de similitud contra el mazo actual; el servidor
      // re-valida todo al confirmar (cliente no es fuente de verdad).
      const check = validateImportData(parsed, customCards)
      if (!check.valid) {
        showAlert(check.message, true)
        return
      }
      if (check.summary.newWhite === 0 && check.summary.newBlack === 0) {
        const { dupes, invalid } = check.summary
        showAlert(`Ninguna carta nueva que importar (${dupes} duplicadas, ${invalid} inválidas).`, true)
        return
      }
      setImportResult(null)
      setPendingImport({ fileName: file.name, data: parsed, check })
    }
    reader.onerror = () => showAlert('No se pudo leer el archivo.', true)
    reader.readAsText(file)
  }

  const confirmImport = () => {
    if (!pendingImport || importing) return
    soundManager.playCard()
    setImporting(true)
    socket.emit('import_custom_cards', { json: pendingImport.data, file_name: pendingImport.fileName })
  }

  const whiteList = customCards.whiteCards || []
  const blackList = customCards.blackCards || []
  const totalCount = whiteList.length + blackList.length

  return {
    cardType, setCardType, cardText, setCardText, cardPick, setCardPick,
    loading, alertInfo, customCards, filterTab, setFilterTab,
    fileInputRef, importing, pendingImport, setPendingImport,
    importResult, whiteList, blackList, totalCount,
    confirmDelete, requestDeleteCard, cancelDeleteCard, confirmDeleteCard,
    showAlert, fetchCustomCards, handleAddCard,
    openFilePicker, handleFileSelected, confirmImport,
  }
}
